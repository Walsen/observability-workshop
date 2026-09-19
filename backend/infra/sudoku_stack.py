"""The CDK stack for the X-Ray Sudoku Demo (task 10.1: the stack shell).

:class:`SudokuStack` is the single CDK ``Stack`` that will define the demo's
backend: the DynamoDB ``Games_Table``, the four Python 3.12 Lambda functions
with X-Ray active tracing, the REST API with active tracing and Amplify-scoped
CORS, least-privilege IAM per Lambda, and a ``CfnOutput`` emitting the API URL
(design "Infrastructure as Code (CDK)").

As of task 10.5 the stack defines the DynamoDB ``Games_Table``, the four Python
3.12 Lambda functions with X-Ray active tracing, the REST API with active
tracing and Amplify-scoped CORS, least-privilege DynamoDB IAM per Lambda, and
the ``ApiEndpointUrl`` output. Keeping the stack importable and synthesizable at
every step lets ``cdk synth`` run offline (it resolves nothing from an account)
so the app entry point and the Justfile ``synth`` recipe stay exercised before
any resource depends on them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_synthetics as synthetics
from constructs import Construct

# The Lambda asset root is the repository root — the directory that *contains*
# the ``backend/`` package — so a handler's import path resolves exactly as it
# does for the test suite and at runtime: ``backend.handlers.new_game`` and its
# ``backend.data`` / ``backend.domain`` imports all resolve from this root. This
# file is ``backend/infra/sudoku_stack.py``, so the repo root is two parents up.
_ASSET_ROOT = Path(__file__).resolve().parent.parent.parent

# Directories under the repo root that must never be copied into the Lambda asset:
# the dev virtualenv and devbox tree, the CDK synth output, VCS metadata, caches,
# the frontend (a separate Amplify deployment), and the specs. Excluding them
# keeps the asset lean and keeps ``from_asset`` — which stages a *local copy* of
# the root at synth time — fast and offline. ``from_asset`` makes no AWS call.
_ASSET_EXCLUDE = [
    ".venv",
    ".devbox",
    "cdk.out",
    ".git",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".hypothesis",
    ".ruff_cache",
    "frontend",
    ".kiro",
]

# Context key an operator can set (``cdk synth -c allowed_origin=...`` or in
# ``cdk.json``) to pin the real Amplify origin CORS is locked to. It is unknown
# until Amplify Hosting is connected manually in the console, so it is supplied
# at synth/deploy time rather than hardcoded. The placeholder default below keeps
# synth offline and is deliberately a concrete localhost origin, never ``*``.
ALLOWED_ORIGIN_CONTEXT_KEY = "allowed_origin"
_DEFAULT_ALLOWED_ORIGIN = "http://localhost:8000"

# Context key an operator sets (``cdk synth -c canary_target_url=...`` or in
# ``cdk.json``) to point the synthetic canary at the live Amplify site. Like
# ``allowed_origin`` it is unknown until Amplify Hosting is connected manually,
# so it is supplied at synth/deploy time rather than hardcoded — and unlike
# ``allowed_origin`` it has **no default**: the canary (and its alarm) are
# created *only* when this context is present, so the existing offline synth,
# which supplies no context, produces the same backend template with no canary
# (Requirement 11.1).
CANARY_TARGET_URL_CONTEXT_KEY = "canary_target_url"

# The directory bundled as the canary's browser-script asset. It holds the
# Node/Puppeteer Synthetics handler ``sudoku_canary.js`` (entry
# ``sudoku_canary.handler``), created in task 14.1. ``Code.from_asset`` stages a
# local copy at synth time — no AWS call, no network — so wiring the canary keeps
# ``cdk synth`` offline.
_CANARY_ASSET_DIR = _ASSET_ROOT / "backend" / "infra" / "canary"

# The Synthetics runtime the canary runs on: the newest ``syn-nodejs-puppeteer``
# runtime available in the pinned aws-cdk-lib (2.270.0). A headless-Chromium /
# Puppeteer runtime is required by the browser script (Requirement 11.5, design
# "Canary type and browser script"); pinning the newest keeps the workshop on a
# current, supported Synthetics runtime.
_CANARY_RUNTIME = synthetics.Runtime.SYNTHETICS_NODEJS_PUPPETEER_13_0

# The canary's schedule: one run every five minutes -> ``rate(5 minutes)``
# (Requirement 11.2).
_CANARY_SCHEDULE_RATE = cdk.Duration.minutes(5)

# The environment-variable name the browser script reads its target URL from
# (``sudoku_canary.js`` -> ``requireTargetUrl`` reads ``process.env.TARGET_URL``).
# The stack passes the operator-configured ``canary_target_url`` in under this
# name so nothing is hardcoded in the script.
_CANARY_TARGET_URL_ENV = "TARGET_URL"

# The success-rate alarm's threshold and evaluation-period count. The canary's
# ``SuccessPercent`` metric is a percentage; alarming when it is **below 100**
# means any failed run (a broken New Game or Solve flow) trips the alarm
# (Requirement 11.7). One evaluation period keeps detection prompt for a demo —
# a single failed 5-minute run raises the alarm rather than waiting for a second.
_CANARY_ALARM_THRESHOLD = 100
_CANARY_ALARM_EVALUATION_PERIODS = 1

# The Python runtime for every handler (Requirement 9.2).
_LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12

# Function timeout and memory. CDK's defaults (3 s / 128 MB) are too tight for a
# Python cold start that imports boto3 + aws_xray_sdk, runs ``patch_all``, and
# then makes a DynamoDB call under an active X-Ray segment — the first invocation
# can exceed 3 s and time out. These give the cold start comfortable headroom
# (the puzzle generate/solve work itself is a few ms) without over-provisioning a
# demo: 10 s is well inside API Gateway's 29 s integration limit, and 256 MB also
# raises the proportional CPU share so the cold start finishes faster.
_LAMBDA_TIMEOUT = cdk.Duration.seconds(10)
_LAMBDA_MEMORY_MB = 256

# The instruction-set architecture for every function AND the X-Ray SDK layer.
# The layer's wheels (aws-xray-sdk + wrapt) are pip-installed locally by
# ``just build-layer`` (see the Justfile / _xray_layer docstring); wrapt ships a
# compiled extension, so the layer's binaries must match the function's
# architecture. Pinning both to ARM64 keeps them consistent and lets the layer be
# built natively on an arm64 host with no cross-compilation. Function and layer
# MUST agree, so this single constant drives both.
_LAMBDA_ARCHITECTURE = lambda_.Architecture.ARM_64

# The local directory staged as the X-Ray SDK Lambda layer asset. It holds a
# ``python/`` subdirectory (the Lambda layer convention: ``python/`` is added to
# ``sys.path``) into which ``just build-layer`` installs ``aws-xray-sdk`` and its
# ``wrapt`` dependency. ``botocore`` — the SDK's other dependency — is already in
# the Lambda runtime alongside ``boto3``, so only these two ship in the layer.
#
# The directory's ``python/`` skeleton is tracked (a ``.keep``) so ``from_asset``
# resolves at synth time on a fresh checkout with no wheels present: synth only
# stages the local directory, it does not need the dependencies installed. The
# wheels are installed at deploy time and are git-ignored. This is what keeps the
# offline synth/test guarantee intact while still giving the deployed functions a
# working ``aws_xray_sdk`` import.
_XRAY_LAYER_DIR = _ASSET_ROOT / "build" / "xray-layer"

# The logical id / key of the ``CfnOutput`` that emits the deployed API endpoint
# URL (Requirement 9.3). Exposed as a module constant so the offline synth
# assertions can look the output up by id rather than duplicating the string.
API_URL_OUTPUT_ID = "ApiEndpointUrl"


class SudokuStack(cdk.Stack):
    """The backend stack: table, Lambdas, REST API, IAM, and the API URL output.

    The standard ``__init__`` signature forwards ``**kwargs`` (notably ``env``,
    pinned to us-east-1 by :mod:`backend.app`) to :class:`aws_cdk.Stack` so the
    stack synthesizes without any account or region lookup.

    Resources are introduced across tasks 10.2–10.5:

    - 10.2 — the DynamoDB ``Games_Table`` (``gameId`` PK, ``playerId`` SK). Done.
    - 10.3 — the four Lambda functions with tracing ``Active``. Done.
    - 10.4 — the REST API with active tracing and Amplify-scoped CORS. Done.
    - 10.5 — least-privilege DynamoDB IAM per Lambda (each granted only the
      actions its handler performs) and the ``ApiEndpointUrl`` output. Done.

    The Games table is exposed as :attr:`games_table` so task 10.3 (which sets
    the ``GAMES_TABLE_NAME`` environment variable on each Lambda) and task 10.5
    (which grants each Lambda the least-privilege DynamoDB actions it performs)
    can reference the one table construct rather than rebuilding it.

    The four Lambda functions are exposed individually (:attr:`new_game_fn`,
    :attr:`get_game_fn`, :attr:`submit_move_fn`, :attr:`solve_fn`) and collected
    in :attr:`functions` keyed by endpoint name, so task 10.4 can wire each to
    its API route and task 10.5 can grant each the actions it performs — without
    either rebuilding the functions.
    """

    #: The DynamoDB table storing one item per ``(gameId, playerId)`` game.
    games_table: dynamodb.Table

    #: The Lambda layer providing ``aws_xray_sdk`` (+ ``wrapt``) to every handler.
    xray_layer: lambda_.LayerVersion

    #: ``POST /games`` — generate a puzzle and start a game.
    new_game_fn: lambda_.Function
    #: ``GET /games/{gameId}`` — load a game's current board.
    get_game_fn: lambda_.Function
    #: ``POST /games/{gameId}/moves`` — validate and apply a move.
    submit_move_fn: lambda_.Function
    #: ``POST /games/{gameId}/solve`` — solve the board.
    solve_fn: lambda_.Function

    #: The four functions keyed by endpoint name, for tasks 10.4/10.5 to iterate.
    functions: dict[str, lambda_.Function]

    #: The REST API fronting the four handlers; exposed so task 10.5 can emit the
    #: ``API_Endpoint_URL`` output from :attr:`api.url`.
    api: apigateway.RestApi

    #: The synthetic browser canary, or ``None`` when no ``canary_target_url``
    #: context was supplied. Created only when the operator provides the live
    #: site URL (task 14.2, Requirement 11.1), so the offline synth without that
    #: context leaves this ``None`` and adds no canary/alarm to the template.
    canary: synthetics.Canary | None

    def __init__(
        self, scope: Construct, construct_id: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.games_table = self._create_games_table()
        self.xray_layer = self._create_xray_layer()
        self._create_lambda_functions()
        self.api = self._create_api()
        self._grant_table_access()
        self._emit_api_url_output()
        # The canary and its alarm are created *only* when an operator supplies
        # the live target URL as context. Absent it, the stack synthesizes with
        # neither — keeping the existing offline backend template unchanged
        # (Requirement 11.1). When present, ``_create_canary`` builds the canary,
        # its auto-provisioned artifacts bucket + least-privilege role, and the
        # ``SuccessPercent`` alarm.
        target_url = self._canary_target_url()
        self.canary = self._create_canary(target_url) if target_url else None

    def _create_games_table(self) -> dynamodb.Table:
        """Create the ``Games_Table`` keyed by ``gameId`` (PK) / ``playerId`` (SK).

        The key design follows the design's "Data Model: Key design"
        (Requirement 9.1): ``gameId`` is the partition key and ``playerId`` the
        sort key, so every endpoint's access is a single point ``GetItem`` /
        ``PutItem`` on one fully-specified ``(gameId, playerId)`` item — no query,
        scan, or GSI. Both key attributes are strings (``S``); the item's board,
        puzzle, and solution are stored as JSON string attributes by the
        repository, so no further attribute definitions belong on the table (only
        key attributes are declared in DynamoDB).

        Billing is on-demand (``PAY_PER_REQUEST``), matching the design's
        "on-demand billing": a workshop demo has spiky, unpredictable traffic and
        no capacity to plan, so per-request billing avoids provisioning entirely.

        The removal policy is ``DESTROY``: this is a throwaway workshop demo, so
        the table (and its data) should be torn down with the stack rather than
        orphaned on ``cdk destroy``. This is deliberately not the production
        default (``RETAIN``) — it is the right choice for a demo whose game state
        is disposable.

        The physical table name is left unset so CloudFormation auto-names it,
        avoiding cross-deploy name collisions; the Lambdas receive the resolved
        name via the ``GAMES_TABLE_NAME`` environment variable (task 10.3) rather
        than hardcoding it here.
        """
        return dynamodb.Table(
            self,
            "GamesTable",
            partition_key=dynamodb.Attribute(
                name="gameId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="playerId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

    def _create_xray_layer(self) -> lambda_.LayerVersion:
        """Create the Lambda layer that supplies ``aws_xray_sdk`` to the handlers.

        The handlers import ``aws_xray_sdk`` (the bootstrap patches ``boto3`` for
        X-Ray at module load), but the Lambda runtime ships only ``boto3`` /
        ``botocore`` — not ``aws-xray-sdk`` — so without this layer every function
        would fail its cold-start import with ``ModuleNotFoundError:
        aws_xray_sdk``. This layer closes that gap while preserving the offline
        synth/test guarantee.

        The layer's code is a **local-directory asset** (:data:`_XRAY_LAYER_DIR`),
        not a Docker bundling step: ``just build-layer`` pip-installs the pinned
        ``aws-xray-sdk`` (and its ``wrapt`` dependency) into that directory's
        ``python/`` subtree before deploy, and ``from_asset`` merely stages the
        directory at synth time — making no AWS call and needing no network. The
        directory's skeleton is tracked so synth resolves it even when the wheels
        are absent, which is exactly the offline case the test suite runs in;
        Docker-based bundling, by contrast, would force the offline suite to have
        Docker, so it is deliberately avoided.

        The layer is pinned to the same architecture as the functions
        (:data:`_LAMBDA_ARCHITECTURE`) because ``wrapt`` carries a compiled
        extension: a function and the layer it loads must share an instruction
        set, so both are declared ARM64 from the one constant.
        """
        return lambda_.LayerVersion(
            self,
            "XRaySdkLayer",
            code=lambda_.Code.from_asset(str(_XRAY_LAYER_DIR)),
            compatible_runtimes=[_LAMBDA_RUNTIME],
            compatible_architectures=[_LAMBDA_ARCHITECTURE],
            description=(
                "aws-xray-sdk (and wrapt) for the game handlers; botocore is "
                "already in the Lambda runtime. Built by `just build-layer`."
            ),
        )

    def _create_lambda_functions(self) -> None:
        """Create the four game Lambda functions and record them for later tasks.

        Each function runs the Python 3.12 runtime with X-Ray tracing ``Active``
        (Requirement 9.2) and receives the resolved table name and the allowed
        CORS origin via environment variables. The four are created through the
        shared :meth:`_create_function` helper so runtime, tracing, asset, and
        environment are configured in exactly one place — adding a fifth endpoint
        is one more call, not a fifth copy of the configuration.

        The functions are stored both individually (``self.new_game_fn`` …) and
        in ``self.functions`` keyed by endpoint name, so task 10.4 can attach
        each to its API route and task 10.5 can grant each only the DynamoDB
        actions it performs.
        """
        self.new_game_fn = self._create_function(
            "NewGameFunction", "backend.handlers.new_game.lambda_handler"
        )
        self.get_game_fn = self._create_function(
            "GetGameFunction", "backend.handlers.get_game.lambda_handler"
        )
        self.submit_move_fn = self._create_function(
            "SubmitMoveFunction", "backend.handlers.submit_move.lambda_handler"
        )
        self.solve_fn = self._create_function(
            "SolveFunction", "backend.handlers.solve.lambda_handler"
        )
        self.functions = {
            "new_game": self.new_game_fn,
            "get_game": self.get_game_fn,
            "submit_move": self.submit_move_fn,
            "solve": self.solve_fn,
        }

    def _create_function(
        self, construct_id: str, handler: str
    ) -> lambda_.Function:
        """Create one game Lambda function with tracing, runtime, and environment.

        ``handler`` is the dotted path to the handler's ``lambda_handler`` entry
        point (e.g. ``backend.handlers.new_game.lambda_handler``); the asset root
        is the repository root, so that ``backend.*`` path resolves the same way
        it does in tests and at runtime.

        Every function is configured identically here:

        - **runtime** ``python3.12`` (Requirement 9.2).
        - **architecture** ``ARM64`` (:data:`_LAMBDA_ARCHITECTURE`), matching the
          X-Ray SDK layer's locally-built ``wrapt`` extension.
        - **timeout / memory** :data:`_LAMBDA_TIMEOUT` / :data:`_LAMBDA_MEMORY_MB`
          — raised from CDK's 3 s / 128 MB defaults, which are too tight for the
          instrumented Python cold start (boto3 + aws_xray_sdk import, ``patch_all``,
          first DynamoDB call under an active segment).
        - **tracing** ``Active`` — X-Ray active tracing (Requirement 9.2). CDK
          attaches the X-Ray write permissions this implies to the auto-created
          execution role; task 10.5 adds the DynamoDB grants (and need not
          re-add X-Ray write, to avoid duplicating the policy).
        - **code** a local-directory asset (see :meth:`_lambda_code`).
        - **layers** the shared X-Ray SDK layer (see :meth:`_create_xray_layer`),
          which supplies the ``aws_xray_sdk`` the handlers import but the runtime
          does not ship. Without it every function would ``ModuleNotFoundError``
          on cold start.
        - **environment** ``GAMES_TABLE_NAME`` (the resolved table name, a
          CloudFormation ``Ref`` to the table) and ``ALLOWED_ORIGIN`` (the
          operator-configured Amplify origin), which the handlers and the
          ``responses`` CORS helper read at runtime.
        """
        return lambda_.Function(
            self,
            construct_id,
            runtime=_LAMBDA_RUNTIME,
            architecture=_LAMBDA_ARCHITECTURE,
            handler=handler,
            code=self._lambda_code(),
            layers=[self.xray_layer],
            timeout=_LAMBDA_TIMEOUT,
            memory_size=_LAMBDA_MEMORY_MB,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "GAMES_TABLE_NAME": self.games_table.table_name,
                "ALLOWED_ORIGIN": self._allowed_origin(),
            },
        )

    def _create_api(self) -> apigateway.RestApi:
        """Create the REST API wiring the four handlers, with tracing and CORS.

        A single :class:`aws_cdk.aws_apigateway.RestApi` fronts the four game
        Lambdas (design "API Gateway (REST) Design"). Two cross-cutting concerns
        are configured on the API itself rather than per method:

        - **X-Ray active tracing on the stage** — ``deploy_options`` sets
          ``tracing_enabled=True`` (Requirement 8.1), so the API records its own
          segment under the incoming trace id and propagates the context to the
          Lambda invocation (Requirement 1.2). This is what lets the API segment
          join the single browser-to-DynamoDB trace.
        - **CORS locked to the Amplify origin** — ``default_cors_preflight_options``
          adds a preflight ``OPTIONS`` method to every resource, advertising the
          single configured origin (never ``*``), the ``GET``/``POST``/``OPTIONS``
          methods, and the ``Content-Type`` and ``X-Amzn-Trace-Id`` headers
          (Requirement 8.2/8.3). ``playerId`` travels in the body/query rather
          than a header, so no ``playerId`` header is advertised. The handlers
          additionally attach CORS to their *actual* proxy responses (via the
          ``responses`` helper); this preflight configuration and that
          actual-response CORS are complementary, and both are wanted.

        Each route is a Lambda proxy integration
        (:class:`aws_cdk.aws_apigateway.LambdaIntegration`) to the matching
        function, so the handler receives the raw proxy event and returns the
        proxy response shape itself (no web framework). Building the ``RestApi``
        and its integrations resolves nothing from an account, so ``cdk synth``
        stays offline.
        """
        api = apigateway.RestApi(
            self,
            "SudokuApi",
            deploy_options=apigateway.StageOptions(tracing_enabled=True),
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=[self._allowed_origin()],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "X-Amzn-Trace-Id"],
            ),
        )
        self._add_routes(api)
        return api

    def _add_routes(self, api: apigateway.RestApi) -> None:
        """Wire the four resources/methods to their handlers as proxy integrations.

        The resource tree mirrors the design's "Resources / methods" table:

        - ``POST /games`` -> ``new_game``
        - ``GET  /games/{gameId}`` -> ``get_game``
        - ``POST /games/{gameId}/moves`` -> ``submit_move``
        - ``POST /games/{gameId}/solve`` -> ``solve``

        Each method is a :class:`LambdaIntegration` to the matching function, so
        the four Lambdas keep their single responsibility (translate the proxy
        event to and from the domain) and the API owns only the routing.
        """
        games = api.root.add_resource("games")
        games.add_method(
            "POST", apigateway.LambdaIntegration(self.new_game_fn)
        )

        game = games.add_resource("{gameId}")
        game.add_method(
            "GET", apigateway.LambdaIntegration(self.get_game_fn)
        )

        moves = game.add_resource("moves")
        moves.add_method(
            "POST", apigateway.LambdaIntegration(self.submit_move_fn)
        )

        solve = game.add_resource("solve")
        solve.add_method(
            "POST", apigateway.LambdaIntegration(self.solve_fn)
        )

    def _grant_table_access(self) -> None:
        """Grant each Lambda only the DynamoDB actions its handler performs.

        Least-privilege IAM per Lambda (Requirement 9.5, design "Infrastructure
        as Code (CDK)"). Each grant uses the table's own grant helper, which
        scopes the generated IAM policy to *this table's* ARN (and its indexes)
        — never ``"*"`` and never account-wide. The grant per function matches
        exactly what its handler does to the one ``(gameId, playerId)`` item:

        - ``get_game`` — ``repository.load`` only, a single ``GetItem``. Read
          only: :meth:`~aws_cdk.aws_dynamodb.Table.grant_read_data`.
        - ``solve`` — ``repository.load`` only; the solve handler computes the
          completion and returns it **without persisting** (task 8.5), so it too
          is read only. If solve ever stored the solution this would need write,
          but it does not, so granting write would be excess privilege.
        - ``new_game`` — ``repository.save`` only, a single ``PutItem`` after
          generating the puzzle; it never loads. Write only:
          :meth:`~aws_cdk.aws_dynamodb.Table.grant_write_data` — the tightest
          fit, rather than the broader read+write.
        - ``submit_move`` — ``repository.load`` **and** ``repository.save``
          (``GetItem`` then ``PutItem``): read + write via
          :meth:`~aws_cdk.aws_dynamodb.Table.grant_read_write_data`.

        X-Ray write permission (``xray:PutTraceSegments`` /
        ``xray:PutTelemetryRecords``) is **not** added here: CDK already attaches
        it to each function's execution role because the function is created with
        ``tracing=ACTIVE`` (see :meth:`_create_function`). Adding it again would
        duplicate an existing managed grant, so the least-privilege change is
        purely the DynamoDB actions.
        """
        # get_game: load only -> read.
        self.games_table.grant_read_data(self.get_game_fn)
        # solve: load only, does not persist (task 8.5) -> read.
        self.games_table.grant_read_data(self.solve_fn)
        # new_game: save only (PutItem after generate) -> write.
        self.games_table.grant_write_data(self.new_game_fn)
        # submit_move: load + save (GetItem + PutItem) -> read + write.
        self.games_table.grant_read_write_data(self.submit_move_fn)

    def _emit_api_url_output(self) -> None:
        """Emit the deployed API endpoint URL as a stack output (Requirement 9.3).

        ``self.api.url`` is a CloudFormation intrinsic that resolves at deploy
        time to the invoke URL of the API's deployed stage (e.g.
        ``https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/``). Emitting
        it as a :class:`~aws_cdk.CfnOutput` keyed :data:`API_URL_OUTPUT_ID` gives
        the operator the value they set as the Amplify configuration that
        ``config.js`` reads — step two of the manual, two-step deploy handoff
        (design "Deployment Handoff"). It resolves nothing from an account at
        synth time, so this keeps ``cdk synth`` offline.
        """
        cdk.CfnOutput(
            self,
            API_URL_OUTPUT_ID,
            value=self.api.url,
            description=(
                "Invoke URL of the deployed REST API stage; set this as the "
                "Amplify API endpoint configuration the frontend reads."
            ),
        )

    def _lambda_code(self) -> lambda_.Code:
        """Return the Lambda code asset: the repo root, minus dev/build cruft.

        ``from_asset`` references a *local directory* and stages a copy of it at
        synth time — it makes no AWS call and needs no network, so ``cdk synth``
        stays fully offline. The asset root is the repository root so the
        handlers' ``backend.*`` imports resolve; :data:`_ASSET_EXCLUDE` prunes the
        virtualenv, caches, VCS metadata, the frontend, and the synth output so
        only the Python sources ship.

        This asset carries **source only** — the handlers import ``aws_xray_sdk``,
        which is *not* in the Lambda runtime (``boto3``/``botocore`` are). That
        third-party dependency is supplied separately by the X-Ray SDK layer (see
        :meth:`_create_xray_layer`), not bundled into this asset, so this stays a
        plain local-directory reference and synth stays offline. Docker-based
        dependency bundling would force the offline suite to have Docker, so it is
        deliberately avoided in favour of the layer.
        """
        return lambda_.Code.from_asset(
            str(_ASSET_ROOT), exclude=_ASSET_EXCLUDE
        )

    def _allowed_origin(self) -> str:
        """Return the CORS origin for the handlers, from context or the default.

        Reads the ``allowed_origin`` CDK context value (set with
        ``cdk synth -c allowed_origin=https://main.<app-id>.amplifyapp.com`` or in
        ``cdk.json``), which the operator sets to the real Amplify origin once
        Amplify Hosting is connected manually in the console — it is unknown until
        then. Absent that, it falls back to :data:`_DEFAULT_ALLOWED_ORIGIN`, a
        concrete localhost origin (never ``*``), which keeps synth offline and
        CORS non-wildcarded even if the operator forgets to set it.
        """
        origin = self.node.try_get_context(ALLOWED_ORIGIN_CONTEXT_KEY)
        if isinstance(origin, str) and origin.strip():
            return origin.strip()
        return _DEFAULT_ALLOWED_ORIGIN

    def _canary_target_url(self) -> str | None:
        """Return the canary's target URL from context, or ``None`` when unset.

        Reads the ``canary_target_url`` CDK context value (set with
        ``cdk synth -c canary_target_url=https://main.<app-id>.amplifyapp.com`` or
        in ``cdk.json``), which the operator sets to the live Amplify origin once
        Amplify Hosting is connected manually in the console. This mirrors
        :meth:`_allowed_origin`'s context read, with one deliberate difference:
        there is **no default**. When the context is absent (or blank) this
        returns ``None``, and :meth:`__init__` then creates neither the canary nor
        the alarm — so the existing offline synth, which supplies no context,
        produces the same backend template as before (Requirement 11.1). A blank
        value is treated as absent so a stray ``-c canary_target_url=`` does not
        create a canary aimed at nowhere.
        """
        target = self.node.try_get_context(CANARY_TARGET_URL_CONTEXT_KEY)
        if isinstance(target, str) and target.strip():
            return target.strip()
        return None

    def _create_canary(self, target_url: str) -> synthetics.Canary:
        """Create the synthetic browser canary and its ``SuccessPercent`` alarm.

        Called only when :meth:`_canary_target_url` returned a URL, so reaching
        here means the operator asked for the canary (Requirement 11.1).

        The canary is the **stable** ``aws_cdk.aws_synthetics`` L2 ``Canary`` that
        ships in the pinned ``aws-cdk-lib`` (2.270.0). The alpha module
        ``aws_cdk.aws_synthetics_alpha`` is **intentionally not used** and not
        added as a dependency — the stable module provides both the L2 ``Canary``
        and the L1 ``CfnCanary``, so no alpha package is needed (design "CDK
        construct: stable aws_synthetics, alpha module intentionally avoided").

        The L2 ``Canary`` is configured with:

        - **runtime** :data:`_CANARY_RUNTIME` — the newest ``syn-nodejs-puppeteer``
          runtime in the pinned lib, a headless-Chromium/Puppeteer runtime the
          browser script needs (Requirement 11.5).
        - **test** ``Test.custom`` over :data:`_CANARY_ASSET_DIR` with handler
          ``sudoku_canary.handler`` — the Node asset from task 14.1.
          ``Code.from_asset`` stages a local copy at synth time (no AWS call),
          keeping synth offline.
        - **schedule** ``Schedule.rate(rate(5 minutes))`` (Requirement 11.2).
        - **active tracing** ``active_tracing=True`` — X-Ray on the canary runs,
          so each scheduled run joins the browser->API->Lambda->DynamoDB trace
          (Requirement 11.5).
        - **environment variables** the operator-supplied ``target_url`` passed in
          as ``TARGET_URL`` (:data:`_CANARY_TARGET_URL_ENV`), which the script
          reads — so no URL is hardcoded in the asset.
        - **provisioned_resource_cleanup** ``True`` — on ``cdk destroy`` the L2
          construct removes the underlying canary Lambda functions and layers
          too, matching the demo's throwaway, ``DESTROY``-everything posture (cf.
          the table's removal policy) rather than leaving orphaned functions
          behind. This is the non-deprecated replacement for the older
          ``cleanup=Cleanup.LAMBDA`` kwarg (deprecated in aws-cdk-lib 2.270.0 in
          favour of ``provisioned_resource_cleanup``); the boolean ``True`` is
          the equivalent of the old ``Cleanup.LAMBDA``.

        The L2 construct **auto-provisions the artifacts S3 bucket and a
        least-privilege execution role** scoped to running the script, writing run
        artifacts, publishing CloudWatch metrics, and writing logs — no blanket
        bucket or account access (Requirements 11.6, 11.8). Those are deliberately
        not hand-rolled here. The one permission that role omits even under active
        tracing is X-Ray write, so :meth:`_grant_canary_xray_write` adds exactly
        that (``xray:PutTraceSegments`` / ``PutTelemetryRecords``) and nothing
        more — without it every run fails ``AccessDeniedException`` before
        producing a result.
        """
        canary = synthetics.Canary(
            self,
            "SudokuCanary",
            runtime=_CANARY_RUNTIME,
            test=synthetics.Test.custom(
                code=synthetics.Code.from_asset(str(_CANARY_ASSET_DIR)),
                handler="sudoku_canary.handler",
            ),
            schedule=synthetics.Schedule.rate(_CANARY_SCHEDULE_RATE),
            active_tracing=True,
            environment_variables={_CANARY_TARGET_URL_ENV: target_url},
            provisioned_resource_cleanup=True,
        )
        self._grant_canary_xray_write(canary)
        self._create_canary_alarm(canary)
        return canary

    def _grant_canary_xray_write(self, canary: synthetics.Canary) -> None:
        """Grant the canary's execution role permission to publish X-Ray segments.

        With ``active_tracing=True`` the Synthetics runtime publishes an X-Ray
        segment for every run so the run joins the browser -> API -> Lambda ->
        DynamoDB trace (Requirement 11.5). But the L2 ``Canary``'s
        auto-provisioned role only covers running the script, writing artifacts
        to S3, publishing the CloudWatch metrics, and writing logs — it does
        **not** add X-Ray write, even when active tracing is on (a gap in the
        pinned aws-cdk-lib's Synthetics L2). Without this grant the runtime's
        ``xray:PutTraceSegments`` call is denied and the whole run fails with
        ``AccessDeniedException`` ("No test result returned"), so the grant is
        required for the canary to run green at all, not merely to emit traces.

        The two actions mirror exactly what CDK attaches to the tracing-enabled
        Lambdas (``AWSXRayDaemonWriteAccess``): ``xray:PutTraceSegments`` and
        ``xray:PutTelemetryRecords``. Neither X-Ray write action supports
        resource-level scoping, so ``resources=["*"]`` is the least privilege the
        actions allow — the same shape the managed X-Ray write policy uses. This
        keeps the role's other permissions untouched and adds only what active
        tracing needs.
        """
        canary.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

    def _create_canary_alarm(self, canary: synthetics.Canary) -> cloudwatch.Alarm:
        """Alarm when the canary's success rate drops below 100% (Requirement 11.7).

        Built on the canary's ``SuccessPercent`` metric via the L2's
        :meth:`~aws_cdk.aws_synthetics.Canary.metric_success_percent` (equivalently
        a ``cloudwatch.Metric`` in the ``CloudWatchSynthetics`` namespace,
        dimension ``CanaryName``). ``SuccessPercent`` is a percentage, so a
        threshold of :data:`_CANARY_ALARM_THRESHOLD` (100) with
        ``LESS_THAN_THRESHOLD`` trips on *any* failed run — a broken New Game or
        Solve flow surfaces automatically rather than silently.

        ``evaluation_periods`` is :data:`_CANARY_ALARM_EVALUATION_PERIODS` (1) so a
        single failed 5-minute run raises the alarm — prompt detection for a demo.
        ``treat_missing_data`` is ``BREACHING``: for a health monitor, a canary
        that stops reporting (its runs are not producing a ``SuccessPercent``
        datapoint at all) is itself a problem that should trip the alarm, not be
        masked as "no news is good news".
        """
        return cloudwatch.Alarm(
            self,
            "SudokuCanarySuccessAlarm",
            metric=canary.metric_success_percent(),
            threshold=_CANARY_ALARM_THRESHOLD,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=_CANARY_ALARM_EVALUATION_PERIODS,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description=(
                "Sudoku synthetic canary success rate dropped below 100% — the "
                "New Game or Solve flow is failing against the live site."
            ),
        )
