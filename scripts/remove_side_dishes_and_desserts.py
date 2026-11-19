"""Preview or apply the #35 menu cleanup using the checked-in food-code list."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.recommend.order_sheet import effective_question_feedback, recalculate_question_weights
from scripts.seed_menus import load_removed_food_codes


ACTIVE_ROUND_STATUSES = ("ANSWERING", "ANSWERING_PAUSED", "VOTING", "VOTING_PAUSED")


def clean_notification_payload(payload, menu_ids):
    if not payload:
        return payload

    def clean_state(state):
        current_round = state.get("current_round")
        if not current_round:
            return state
        clean_round = dict(current_round)
        for field in ("candidates", "winners"):
            if field in clean_round:
                clean_round[field] = [
                    item for item in clean_round[field] if item.get("menu_id") not in menu_ids
                ]
        return {**state, "current_round": clean_round}

    cleaned = clean_state(payload)
    if payload.get("common_state"):
        cleaned = {**cleaned, "common_state": clean_state(payload["common_state"])}
    return cleaned


def clean_menu_records(connection, food_codes, *, apply=False):
    inspector = inspect(connection)
    metadata = MetaData()
    menus = Table("menus", metadata, autoload_with=connection)
    menu_query = select(menus.c.id).where(menus.c.food_code.in_(food_codes))
    menu_ids = set(connection.scalars(menu_query.with_for_update() if apply else menu_query))
    report = {"menus": len(menu_ids)}
    if not menu_ids:
        return report

    def table(name):
        if inspector.has_table(name):
            return Table(name, metadata, autoload_with=connection)
        return None

    rounds = table("room_rounds")
    members = table("room_round_members")
    candidates = table("room_round_candidates")
    candidate_ids = set()
    affected_round_ids = set()
    if candidates is not None:
        for candidate_id, round_id in connection.execute(
            select(candidates.c.id, candidates.c.round_id).where(candidates.c.menu_id.in_(menu_ids))
        ):
            candidate_ids.add(candidate_id)
            affected_round_ids.add(round_id)
    if members is not None:
        affected_round_ids.update(connection.scalars(
            select(members.c.round_id).where(members.c.top_menu_id.in_(menu_ids))
        ))
    if rounds is not None and affected_round_ids:
        active_rounds = list(connection.scalars(
            select(rounds.c.id).where(
                rounds.c.id.in_(affected_round_ids), rounds.c.status.in_(ACTIVE_ROUND_STATUSES)
            ).order_by(rounds.c.id)
        ))
        report["affected_active_rounds"] = active_rounds
        if apply and active_rounds:
            raise ValueError(f"삭제 대상 메뉴가 있는 진행 중 라운드: {active_rounds}")

    def delete_rows(target, condition):
        if apply:
            return connection.execute(target.delete().where(condition)).rowcount
        return connection.scalar(select(func.count()).select_from(target).where(condition))

    removed_feedback_question_ids = set()
    if inspector.has_table("order_sheet_questions") and inspector.has_table("recommendation_feedbacks"):
        with Session(bind=connection) as db:
            removed_feedback_question_ids = {
                int(question_id)
                for feedback in effective_question_feedback(db)
                if feedback.selected_menu_id in menu_ids
                for question_id in feedback.answer_match_results
                if str(question_id).isdigit()
            }
    report["questions_with_removed_feedback"] = len(removed_feedback_question_ids)

    operations = table("request_operations")
    report["request_operations"] = 0
    for name, column, operation_scope in (
        ("user_meals", "menu_id", "USER_MEAL_CREATE"),
        ("recommendation_item_snapshots", "menu_id", None),
        ("recommendation_feedback_states", "selected_menu_id", None),
        ("recommendation_feedbacks", "selected_menu_id", "RECOMMENDATION_FEEDBACK"),
    ):
        related = table(name)
        if related is not None:
            matches = related.c[column].in_(menu_ids)
            if operations is not None and operation_scope:
                record_ids = [str(value) for value in connection.scalars(select(related.c.id).where(matches))]
                if record_ids:
                    report["request_operations"] += delete_rows(
                        operations,
                        (operations.c.scope == operation_scope) & operations.c.resource_id.in_(record_ids),
                    )
            report[name] = delete_rows(related, matches)

    recommendations = table("recommendations")
    if recommendations is not None:
        report["recommendations_updated"] = 0
        rows = connection.execute(
            select(recommendations.c.id, recommendations.c.recommended_menu_ids)
        ).all()
        for record_id, previous_ids in rows:
            if not previous_ids or menu_ids.isdisjoint(previous_ids):
                continue
            remaining = [menu_id for menu_id in previous_ids if menu_id is not None and menu_id not in menu_ids]
            if remaining and (previous_ids[0] is None or previous_ids[0] in menu_ids):
                # Preserve the missing first position so implicit feedback cannot move to a different menu.
                remaining.insert(0, None)
            report["recommendations_updated"] += 1
            if apply:
                connection.execute(recommendations.update().where(
                    recommendations.c.id == record_id
                ).values(recommended_menu_ids=remaining))

    if members is not None and affected_round_ids:
        top_menu_matches = members.c.top_menu_id.in_(menu_ids)
        report["room_round_top_menus_cleared"] = connection.scalar(
            select(func.count()).select_from(members).where(top_menu_matches)
        )
        if apply:
            connection.execute(members.update().where(top_menu_matches).values(top_menu_id=None))

        report["room_round_votes_updated"] = 0
        rows = connection.execute(select(members.c.id, members.c.voted_candidate_ids).where(
            members.c.round_id.in_(affected_round_ids), members.c.voted_candidate_ids.is_not(None)
        )).all()
        for member_id, votes in rows:
            remaining = [candidate_id for candidate_id in (votes or []) if candidate_id not in candidate_ids]
            if remaining != (votes or []):
                report["room_round_votes_updated"] += 1
                if apply:
                    connection.execute(members.update().where(
                        members.c.id == member_id
                    ).values(voted_candidate_ids=remaining))

    if candidates is not None and candidate_ids:
        proposers = table("room_round_candidate_proposers")
        if proposers is not None:
            report[proposers.name] = delete_rows(proposers, proposers.c.candidate_id.in_(candidate_ids))
        report[candidates.name] = delete_rows(candidates, candidates.c.id.in_(candidate_ids))

    notifications = table("room_notifications")
    if notifications is not None:
        report["room_notifications_updated"] = 0
        rows = connection.execute(select(notifications.c.id, notifications.c.payload_json).where(
            notifications.c.payload_json.is_not(None)
        )).all()
        for notification_id, payload in rows:
            cleaned = clean_notification_payload(payload, menu_ids)
            if cleaned != payload:
                report["room_notifications_updated"] += 1
                if apply:
                    connection.execute(notifications.update().where(
                        notifications.c.id == notification_id
                    ).values(payload_json=cleaned))

    if apply:
        if removed_feedback_question_ids:
            with Session(bind=connection) as db:
                recalculate_question_weights(db, commit=False, removed_feedback_question_ids=removed_feedback_question_ids)
        connection.execute(menus.delete().where(menus.c.id.in_(menu_ids)))
    return report


def main():
    parser = argparse.ArgumentParser(description="단독 반찬·디저트와 연결된 기록을 확인하거나 삭제합니다.")
    parser.add_argument("--apply", action="store_true", help="DB에 실제 삭제를 적용합니다.")
    args = parser.parse_args()

    engine = create_engine(settings.DATABASE_URL)
    try:
        with engine.begin() as connection:
            report = clean_menu_records(connection, load_removed_food_codes(), apply=args.apply)
            artifact = Path(settings.PERSONAL_MODEL_ARTIFACT_PATH)
            if artifact.is_file() and any(report.get(table_name, 0) for table_name in (
                "user_meals", "recommendation_feedbacks", "recommendation_feedback_states",
                "recommendation_item_snapshots",
            )):
                report["active_personal_model_found"] = True
                if args.apply:
                    archived = artifact.with_name(f"{artifact.name}.before-menu-cleanup-{uuid4().hex}")
                    # Fall back to live signals until a cleaned model is trained and published.
                    artifact.rename(archived)
                    report["archived_personal_model"] = str(archived)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    finally:
        engine.dispose()
    print(json.dumps({"applied": args.apply, **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
