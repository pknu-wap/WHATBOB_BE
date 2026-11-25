"""Inspect, deploy, and register food-kind menu illustrations.

Supports:
  --status
  --kind-key <key> --source-png <path> [--filename <name>.png] [--require-transparency]
  --kind-key <key> --image-url </static/menu-illustrations/...>
  --sync-deployed [--overwrite-existing]
"""
import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.users.models  # noqa: F401
from app.core.database import SessionLocal
from app.menus.food_kinds import DEFAULT_MENU_ILLUSTRATION_URL
from app.menus.illustrations import (
    MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68,
    PENDING_VERIFICATION_ILLUSTRATION_KIND_KEYS,
    IllustrationAuditStatus,
    MenuIllustrationVerificationError,
    deploy_and_register_food_kind_png,
    inspect_food_kind_illustration_status,
    register_food_kind_illustration,
    sync_food_kind_representative_illustrations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show illustration audit status, disk presence, and live effective_image_url for all 8 canonical food kinds.",
    )
    parser.add_argument(
        "--sync-deployed",
        action="store_true",
        help="Register deployed illustrations, preferring SVG and upgrading mapped PNG paths to SVG.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Used with --sync-deployed to also replace custom representative_image_url paths.",
    )
    parser.add_argument(
        "--kind-key",
        type=str,
        default=None,
        help="Target food_kinds.kind_key (e.g. gukbap, jjajangmyeon, pasta).",
    )
    parser.add_argument(
        "--source-png",
        type=Path,
        default=None,
        help="Path to designer-supplied PNG file to validate, deploy atomically, and register in DB.",
    )
    parser.add_argument(
        "--filename",
        type=str,
        default=None,
        help="Optional target .png filename under menu-illustrations/ (used with --source-png; defaults to <kind_key>.png).",
    )
    parser.add_argument(
        "--require-transparency",
        action="store_true",
        help="Used with --source-png to reject PNG files without an alpha/transparency channel.",
    )
    parser.add_argument(
        "--image-url",
        type=str,
        default=None,
        help="Already-deployed relative static URL (/static/menu-illustrations/...) to verify on disk and register in DB.",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help="Optional static root directory override (defaults to app/static).",
    )
    parser.add_argument(
        "--verify-base-url",
        type=str,
        default=None,
        help="Optional running server base URL (e.g. http://127.0.0.1:8000) to verify HTTP 200 reachability before DB update.",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    modes_selected = sum(
        bool(flag)
        for flag in (args.status, args.sync_deployed, args.kind_key is not None)
    )
    if modes_selected != 1:
        parser.error("Specify exactly one mode: --status, --sync-deployed, or --kind-key.")

    if args.overwrite_existing and not args.sync_deployed:
        parser.error("--overwrite-existing can only be used with --sync-deployed.")

    if args.kind_key is None:
        if args.source_png is not None or args.image_url is not None:
            parser.error("--source-png and --image-url require --kind-key.")
        if args.filename is not None or args.require_transparency:
            parser.error("--filename and --require-transparency require --kind-key and --source-png.")
        if args.verify_base_url is not None:
            parser.error("--verify-base-url requires --kind-key.")
        return

    if bool(args.source_png is not None) == bool(args.image_url is not None):
        parser.error("--kind-key requires exactly one of --source-png or --image-url.")

    if args.source_png is None and (args.filename is not None or args.require_transparency):
        parser.error("--filename and --require-transparency can only be used with --source-png.")


def execute_command(db: Session, args: argparse.Namespace) -> dict[str, Any]:
    if args.status:
        items = inspect_food_kind_illustration_status(db, static_dir=args.static_dir)
        pending_for_issue68 = [
            item.kind_key
            for item in items
            if item.audit_status is IllustrationAuditStatus.MISSING_ORIGINAL
            and item.using_common_fallback
        ]
        return {
            "action": "status",
            "default_illustration_url": DEFAULT_MENU_ILLUSTRATION_URL,
            "pending_verification": list(PENDING_VERIFICATION_ILLUSTRATION_KIND_KEYS),
            "missing_for_issue68": list(MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68),
            "pending_for_issue68": pending_for_issue68,
            "items": [item.to_dict() for item in items],
        }

    if args.sync_deployed:
        summary = sync_food_kind_representative_illustrations(
            db,
            static_dir=args.static_dir,
            overwrite_existing=args.overwrite_existing,
            commit=True,
        )
        return {
            "action": "sync_deployed",
            **summary,
        }

    if args.source_png is not None:
        result = deploy_and_register_food_kind_png(
            db,
            kind_key=args.kind_key,
            source_png=args.source_png,
            filename=args.filename,
            static_dir=args.static_dir,
            require_transparency=args.require_transparency,
            verify_http=True,
            verify_base_url=args.verify_base_url,
            commit=True,
        )
        return {
            "action": "deploy_and_register",
            **result.to_dict(),
        }

    food_kind = register_food_kind_illustration(
        db,
        kind_key=args.kind_key,
        image_url=args.image_url,
        static_dir=args.static_dir,
        verify_file_exists=True,
        verify_http=True,
        verify_base_url=args.verify_base_url,
        commit=True,
    )
    return {
        "action": "register_existing",
        "kind_key": food_kind.kind_key,
        "food_kind_id": int(food_kind.id),
        "name": food_kind.name,
        "image_url": food_kind.representative_image_url,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    try:
        with SessionLocal() as db:
            payload = execute_command(db, args)
        print(json.dumps(payload, indent=2))
    except (ValueError, FileNotFoundError, OSError, SQLAlchemyError, MenuIllustrationVerificationError) as exc:
        raise SystemExit(f"Menu illustration registration failed: {exc}") from None


if __name__ == "__main__":
    main()
