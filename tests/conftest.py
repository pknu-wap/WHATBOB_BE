import os

# The ordinary suite must never use a developer's .env database or signing key.
# MySQL integration tests explicitly opt in through their dedicated test URL.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "whatbob-isolated-tests-signing-key-only"
os.environ["KAKAO_REST_API_KEY"] = "test-kakao-key"
