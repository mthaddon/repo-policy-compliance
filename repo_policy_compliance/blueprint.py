# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides API blueprint for flask to run the policy checks.

Note that this blueprint requires the application to be run with a single worker due to the use of
an in-memory set to store the one time tokens. This is done to reduce the complexity of deployments
as the alternative would be to require a database.
"""

import json
import logging
import os
import secrets
import tempfile
from enum import Enum
from hmac import compare_digest
from pathlib import Path
from typing import cast

from flask import Blueprint, Response, request
from flask_httpauth import HTTPTokenAuth
from flask_pydantic import validate

from . import (
    PullRequestInput,
    Result,
    UsedPolicy,
    WorkflowDispatchInput,
    policy,
    pull_request,
    workflow_dispatch,
)

repo_policy_compliance = Blueprint("repo_policy_compliance", __name__)
auth = HTTPTokenAuth(scheme="Bearer")
# Using local variables means that this blueprint can only be used with a single worker. This is
# done to reduce deployment complexity as a database would otherwise be required.
runner_tokens: set[str] = set()
# Need temporary file to persist policy document so better not wrap the entire module in a with
# statement
policy_document_file = tempfile.NamedTemporaryFile()  # pylint: disable=consider-using-with
policy_document_path = Path(policy_document_file.name)

# Bandit thinks this is the token value when it is the name of the environment variable with the
# token value
CHARM_TOKEN_ENV_NAME = "CHARM_TOKEN"  # nosec
# Bandit thinks this is the token value when it is the name of the endpoint to get a one time token
ONE_TIME_TOKEN_ENDPOINT = "/one-time-token"  # nosec
POLICY_ENDPOINT = "/policy"
CHECK_RUN_ENDPOINT = "/check-run"
PULL_REQUEST_CHECK_RUN_ENDPOINT = "/pull_request/check-run"
WORKFLOW_DISPATCH_CHECK_RUN_ENDPOINT = "/workflow_dispatch/check-run"


class Users(str, Enum):
    """The possible users.

    Attrs:
        CHARM: The charm user that can request one time tokens.
        RUNNER: The runner user that can check whether a run should proceed.
    """

    CHARM = "charm"
    RUNNER = "runner"


CHARM_ROLE = Users.CHARM
RUNNER_ROLE = Users.RUNNER


@auth.verify_token
def verify_token(token: str) -> str | None:
    """Verify the authentication token.

    Args:
        token: The token to check.

    Returns:
        The identity associated with the token or None if no token matches.
    """
    charm_token = os.getenv(CHARM_TOKEN_ENV_NAME)

    if not charm_token:
        logging.error(
            (
                "%s environment variable is required for generating one time tokens, it is not "
                "defined or empty"
            ),
            CHARM_TOKEN_ENV_NAME,
        )
        return None

    if compare_digest(token, charm_token):
        return Users.CHARM

    if token in runner_tokens:
        runner_tokens.remove(token)
        return Users.RUNNER

    return None


@auth.get_user_roles
def get_user_roles(user: str) -> str | None:
    """Get the roles of a user.

    Args:
        user: The name of the user.

    Returns:
        The role of the user if they have one, else None.
    """
    match user:
        case Users.CHARM:
            return CHARM_ROLE
        case Users.RUNNER:
            return RUNNER_ROLE
        # It shouldn't be possible to get here since each valid token should be associated with
        # a user
        case _:  # pragma: no cover
            return None


@repo_policy_compliance.route(ONE_TIME_TOKEN_ENDPOINT)
@auth.login_required(role=CHARM_ROLE)
def one_time_token() -> str:
    """Generate a one time token for a runner.

    Returns:
        The one time token.
    """
    token = secrets.token_hex(32)
    runner_tokens.add(token)
    return token


@repo_policy_compliance.route(POLICY_ENDPOINT, methods=["POST"])
@auth.login_required(role=CHARM_ROLE)
def policy_endpoint() -> Response:
    """Generate a one time token for a runner.

    Returns:
        Either that the policy was updated or an error if the policy is invalid.
    """
    data = cast(dict, request.json)
    if not (policy_report := policy.check(document=data)).result:
        return Response(response=policy_report.reason, status=400)

    policy_document_path.write_text(json.dumps(data), encoding="utf-8")
    return Response(status=204)


def _get_policy_document() -> dict | UsedPolicy:
    """Get the current policy document.

    Returns:
        The current policy document if set or that all policies should be used.
    """
    if stored_policy_document_contents := policy_document_path.read_text(encoding="utf-8"):
        return cast(dict, json.loads(stored_policy_document_contents))
    return UsedPolicy.ALL


# Keeping /check-run pointing to this for backwards compatibility
@repo_policy_compliance.route(CHECK_RUN_ENDPOINT, methods=["POST"])
@repo_policy_compliance.route(PULL_REQUEST_CHECK_RUN_ENDPOINT, methods=["POST"])
@auth.login_required(role=RUNNER_ROLE)
@validate()
def pull_request_check_run(body: PullRequestInput) -> Response:
    """Check whether a pull request run should proceed.

    Args:
        body: The request body after it is validated.

    Returns:
        Either to proceed with the run or an error not to proceed with a reason why.
    """
    policy_document = _get_policy_document()

    if (
        report := pull_request(input_=body, policy_document=policy_document)
    ).result == Result.FAIL:
        return Response(response=report.reason, status=403)

    return Response(status=204)


@repo_policy_compliance.route(WORKFLOW_DISPATCH_CHECK_RUN_ENDPOINT, methods=["POST"])
@auth.login_required(role=RUNNER_ROLE)
@validate()
def workflow_dispatch_check_run(body: WorkflowDispatchInput) -> Response:
    """Check whether a workflow dispatch run should proceed.

    Args:
        body: The request body after it is validated.

    Returns:
        Either to proceed with the run or an error not to proceed with a reason why.
    """
    policy_document = _get_policy_document()

    if (
        report := workflow_dispatch(input_=body, policy_document=policy_document)
    ).result == Result.FAIL:
        return Response(response=report.reason, status=403)

    return Response(status=204)
