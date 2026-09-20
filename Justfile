# X-Ray Sudoku Demo — command surface.
#
# `just` is the single documented way to run each task, so a contributor and CI
# invoke the same code path (see dev-environment.md, "just is the command
# surface"). Run every recipe inside the devbox environment (direnv activates it
# on `cd`, or use `devbox run -- just <recipe>`).
#
# Python tooling runs through `uv run`, so it executes in the project virtual
# environment uv owns — never against a globally installed interpreter.
#
# node-on-PATH: `cdk synth` and the offline CDK assertion tests (`import
# aws_cdk`) drive jsii, which needs a bare `node` on PATH. devbox does not expose
# one, but the pinned aws-cdk-cli bundles nodejs in its closure. That node is
# surfaced onto PATH by the devbox `init_hook` (see devbox.json), derived
# dynamically from the `cdk` launcher's shebang — so it tracks aws-cdk-cli
# upgrades and needs no `nodejs` entry in devbox.json. The recipes below rely on
# that wiring and stay free of node plumbing.

# List available recipes.
default:
    @just --list

# Run the test suite (pytest) in the project venv. Fully offline: no AWS
# credentials and no network beyond localhost, including CDK synth and the
# assertions over the synthesized template.
test:
    uv run pytest

# Lint with ruff.
lint:
    uv run ruff check

# Static type-check the backend with mypy.
typecheck:
    uv run mypy backend

# Synthesize the CloudFormation template from the CDK app. Resolves nothing from
# an AWS account (offline); needs `node` on PATH, supplied by the devbox
# init_hook described above.
synth:
    cdk synth --app "uv run python backend/app.py"

# Build the aws-xray-sdk Lambda layer into build/xray-layer/python.
#
# The handlers import aws_xray_sdk, which the Lambda runtime does NOT ship (only
# boto3/botocore). SudokuStack attaches this directory as a Lambda layer; this
# recipe fills it with the pinned aws-xray-sdk (2.15.0, matching uv.lock) and its
# wrapt dependency. botocore is already in the runtime, so it is not installed
# here. The install targets the Lambda's runtime + architecture (python3.12,
# arm64, manylinux) so wrapt's compiled wheel matches the deployed function.
#
# This step needs PyPI and is therefore NOT part of the offline `just test`. The
# offline suite synthesizes against the tracked directory skeleton without the
# wheels present; only a real deploy needs them, so `deploy` runs this first.
#
# --no-deps: aws-xray-sdk declares botocore + wrapt. botocore (and its own tree:
# jmespath, python-dateutil, six, urllib3) is already in the Lambda runtime, so
# shipping it in the layer is dead weight. We install with --no-deps and add back
# only wrapt — the one dependency the runtime lacks. Both are pinned.
build-layer:
    mkdir -p build/xray-layer/python
    # Clear previously-installed packages but preserve the tracked .keep, which
    # is what keeps the asset directory resolvable for the offline synth suite.
    find build/xray-layer/python -mindepth 1 -not -name .keep -delete
    uv pip install --python-version 3.12 --python-platform aarch64-manylinux2014 --only-binary=:all: --no-deps --target build/xray-layer/python "aws-xray-sdk==2.15.0" "wrapt==2.4.1"

# Deploy the stack with the CDK CLI. Emits the API_Endpoint_URL CfnOutput.
# Builds the X-Ray SDK layer first so the deployed functions can import
# aws_xray_sdk. Requires AWS credentials (not part of the offline suite).
deploy: build-layer
    cdk deploy --app "uv run python backend/app.py"

# Verify end-to-end X-Ray tracing against the DEPLOYED stack (task 13).
#
# Drives newGame + solve through the live API with a browser-shaped, originated
# X-Amzn-Trace-Id, then polls X-Ray and asserts one trace spans API Gateway ->
# Lambda -> DynamoDB with the solver subsegments. Resolves the API URL from
# --api-url / API_URL / the XraySudokuDemoStack ApiEndpointUrl output.
#
# Requires AWS credentials and the network — this is EXPLICITLY OUTSIDE the
# offline `just test` suite (design "Testing Strategy"): it is never run by CI's
# credential-free suite and drives a real deployment.
verify-trace:
    uv run python scripts/verify_trace.py

# Report player statistics from the DEPLOYED Games table (task 16).
#
# Runs a read-only, paginated, projected scan of the Games table and prints the
# distinct-player count, total games, games-by-status, and an ESTIMATED
# synthetic (canary) vs organic split derived from the ~5-minute canary cadence
# (a documented heuristic, not a measured fact). The `get-players` skill drives
# this recipe. Pass through flags, e.g. `just player-stats --json` or
# `just player-stats --table-name <name>`.
#
# Requires AWS credentials and the network — like `verify-trace`, this is
# EXPLICITLY OUTSIDE the offline `just test` suite. What is offline-tested is the
# pure aggregation core and the scan/table-resolution plumbing, exercised with
# fakes in backend/tests/test_player_stats.py.
player-stats *args:
    uv run python scripts/player_stats.py {{args}}
