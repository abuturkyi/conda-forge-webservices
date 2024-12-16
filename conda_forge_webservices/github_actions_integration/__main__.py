import json
import logging
import os
import pprint
import subprocess
import sys
import tempfile
import traceback

import click
from conda_forge_feedstock_ops import setup_logging
from conda_forge_feedstock_ops.lint import lint as lint_feedstock
from git import Repo

from .automerge import automerge_pr
from .utils import (
    comment_and_push_if_changed,
    dedent_with_escaped_continue,
    flush_logger,
    get_gha_run_link,
    get_git_patch_relative_to_commit,
    mark_pr_as_ready_for_review,
)
from .api_sessions import create_api_sessions, create_api_sessions_for_admin
from .rerendering import rerender
from .linting import (
    make_lint_comment,
    build_and_make_lint_comment,
    set_pr_status,
    get_recipes_for_linting,
)
from .version_updating import update_version, update_pr_title
from conda_forge_webservices.commands import set_rerender_pr_status


LOGGER = logging.getLogger(__name__)


def _pull_docker_image():
    try:
        print("::group::docker image pull", flush=True)
        subprocess.run(
            [
                "docker",
                "pull",
                f"{os.environ['CF_FEEDSTOCK_OPS_CONTAINER_NAME']}:{os.environ['CF_FEEDSTOCK_OPS_CONTAINER_TAG']}",
            ],
        )
        sys.stderr.flush()
        sys.stdout.flush()
    finally:
        print("::endgroup::", flush=True)


@click.command(name="conda-forge-webservices-run-task")
@click.option("--task", required=True, type=str)
@click.option("--repo", required=True, type=str)
@click.option("--pr-number", required=True, type=str)
@click.option("--task-data-dir", required=True, type=str)
@click.option("--requested-version", required=False, type=str, default=None)
@click.option("--sha", required=False, type=str, default=None)
def main_run_task(task, repo, pr_number, task_data_dir, requested_version, sha):
    setup_logging(level="DEBUG")

    action_desc = f"task `{task}` for conda-forge/{repo}#{pr_number}"
    LOGGER.info(action_desc)
    print(
        f"::notice title=conda-forge-webservices job information::{action_desc}",
        flush=True,
    )

    feedstock_dir = os.path.join(
        task_data_dir,
        repo,
    )
    os.makedirs(feedstock_dir, exist_ok=True)
    repo_url = f"https://github.com/conda-forge/{repo}.git"
    git_repo = Repo.clone_from(
        repo_url,
        feedstock_dir,
    )
    git_repo.remotes.origin.fetch([f"pull/{pr_number}/head:pull/{pr_number}/head"])
    git_repo.git.switch(f"pull/{pr_number}/head")
    prev_head = git_repo.active_branch.commit.hexsha

    task_data = {
        "task": task,
        "repo": repo,
        "pr_number": pr_number,
        "sha": sha,
        "task_results": {},
    }

    if task == "rerender":
        _pull_docker_image()
        changed, rerender_error, info_message, commit_message = rerender(git_repo)
        patch = get_git_patch_relative_to_commit(git_repo, prev_head)
        task_data["task_results"]["changed"] = changed
        task_data["task_results"]["rerender_error"] = rerender_error
        task_data["task_results"]["info_message"] = info_message
        task_data["task_results"]["commit_message"] = commit_message
        task_data["task_results"]["patch"] = patch
    elif task == "version_update":
        if (
            requested_version.lower() == "null"
            or requested_version.lower() == "none"
            or not requested_version
        ):
            requested_version = None

        LOGGER.info(
            "version update requested version: '%s'",
            requested_version,
        )
        _pull_docker_image()
        full_repo_name = f"conda-forge/{repo}"
        version_changed, version_error, new_version = update_version(
            git_repo,
            full_repo_name,
            input_version=requested_version,
        )
        task_data["task_results"]["version_changed"] = version_changed
        task_data["task_results"]["version_error"] = version_error
        task_data["task_results"]["new_version"] = new_version
        task_data["task_results"]["patch"] = None

        if version_changed:
            task_data["task_results"]["commit_message"] = (
                f"ENH: updated version to {new_version}"
            )

            rerender_changed, rerender_error, info_message, commit_message = rerender(
                git_repo
            )
            task_data["task_results"]["rerender_changed"] = rerender_changed
            task_data["task_results"]["rerender_error"] = rerender_error
            task_data["task_results"]["info_message"] = info_message
            if rerender_changed:
                task_data["task_results"]["commit_message"] += (
                    " & " + commit_message[len("MNT: ") :]
                )
            patch = get_git_patch_relative_to_commit(git_repo, prev_head)
            task_data["task_results"]["patch"] = patch
        else:
            task_data["task_results"]["rerender_changed"] = False
            task_data["task_results"]["rerender_error"] = False
            task_data["task_results"]["info_message"] = None
            task_data["task_results"]["commit_message"] = None

    elif task == "lint":
        _pull_docker_image()
        try:
            res = lint_feedstock(feedstock_dir, use_container=True)
            if len(res) == 2:
                lints, hints = res
                all_keys = set(lints.keys()) | set(hints.keys())
                errors = {key: False for key in all_keys}
            else:
                lints, hints, errors = res
            lint_error = False
        except Exception as err:
            LOGGER.warning("LINTING ERROR: %s", repr(err))
            LOGGER.warning("LINTING ERROR TRACEBACK: %s", traceback.format_exc())
            lint_error = True
            lints = None
            hints = None
            errors = None

        task_data["task_results"]["lint_error"] = lint_error
        task_data["task_results"]["lints"] = lints
        task_data["task_results"]["hints"] = hints
        task_data["task_results"]["errors"] = errors
    else:
        raise ValueError(f"Task `{task}` is not valid!")

    with open(os.path.join(task_data_dir, "task_data.json"), "w") as f:
        json.dump(task_data, f)

    subprocess.run(
        ["rm", "-rf", os.path.join(feedstock_dir, ".git")],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["rm", "-rf", feedstock_dir],
        check=True,
        capture_output=True,
    )


def _push_changes(
    *,
    action,
    action_error,
    info_message,
    changed,
    git_repo,
    pr,
    pr_branch,
    pr_owner,
    pr_repo,
    repo_name,
    close_pr_if_no_changes_or_errors,
):
    more_info_message = "\n" + dedent_with_escaped_continue(
        """
        The following suggestions might help debug any issues:
        * Is the `recipe/{{meta.yaml,recipe.yaml}}` file valid?
        * If there is a `recipe/conda-build-config.yaml` file in \\
        the feedstock make sure that it is compatible with the current \\
        [global pinnnings]({}).
        * Is the fork used for this PR on an organization or user GitHub account? \\
        Automated rerendering via the webservices admin bot only works for user \\
        GitHub accounts.
    """.format(
            "https://github.com/conda-forge/conda-forge-pinning-feedstock/"
            "blob/master/recipe/conda_build_config.yaml"
        )
    )
    if action_error:
        if info_message is None:
            info_message = ""
        info_message += more_info_message

    push_error = comment_and_push_if_changed(
        action=action,
        changed=changed,
        error=action_error,
        git_repo=git_repo,
        pull=pr,
        pr_branch=pr_branch,
        pr_owner=pr_owner,
        pr_repo=pr_repo,
        repo_name=repo_name,
        close_pr_if_no_changes_or_errors=close_pr_if_no_changes_or_errors,
        help_message=(
            " or you can try [rerendering locally]"
            "(https://conda-forge.org/docs/maintainer/updating_pkgs.html"
            "#rerendering-with-conda-smithy-locally)"
        ),
        info_message=info_message,
    )

    return action_error or push_error


@click.command(name="conda-forge-webservices-finalize-task")
@click.option("--task-data-dir", required=True, type=str)
def main_finalize_task(task_data_dir):
    logging.basicConfig(level=logging.INFO)

    with open(os.path.join(task_data_dir, "task_data.json")) as f:
        task_data = json.load(f)

    task = task_data["task"]
    repo = task_data["repo"]
    pr_number = task_data["pr_number"]
    task_results = task_data["task_results"]
    sha_for_status = task_data["sha"]

    LOGGER.info("finalizing task `%s` for conda-forge/%s#%s", task, repo, pr_number)
    LOGGER.info("task results:")
    flush_logger(LOGGER)
    print(pprint.pformat(task_results), flush=True)
    flush_logger(LOGGER)

    with tempfile.TemporaryDirectory() as tmpdir:
        full_repo_name = f"conda-forge/{repo}"
        _, gh = create_api_sessions()
        gh_repo = gh.get_repo(full_repo_name)
        pr = gh_repo.get_pull(int(pr_number))

        if task in ["rerender", "version_update", "lint"]:
            if pr.state == "closed":
                LOGGER.error(
                    "Closed PRs cannot be linted, rerendered, "
                    " or have their versions updated! Exiting..."
                )
                return

        # commit the changes if needed
        if task in ["rerender", "version_update"]:
            pr_branch = pr.head.ref
            pr_owner = pr.head.repo.owner.login
            pr_repo = pr.head.repo.name
            if (
                task_results["patch"] is not None
                and task_results["commit_message"] is None
            ):
                LOGGER.warning(
                    "The webservices tasks did not provide a commit message "
                    "but did provide a patch. This is likely an error. "
                    "Proceeding with a default commit message."
                )
                task_results["commit_message"] = "chore: conda-forge-webservices update"

            # always clone just in case we need it
            feedstock_dir = os.path.join(
                tmpdir,
                pr_repo,
            )
            git_repo = Repo.clone_from(
                f"https://github.com/{pr_owner}/{pr_repo}.git",
                feedstock_dir,
                branch=pr_branch,
            )
            if task_results["patch"] is not None:
                patch_file = os.path.join(tmpdir, "rerender-diff.patch")
                with open(patch_file, "w") as fp:
                    fp.write(task_results["patch"])
                subprocess.run(
                    ["git", "apply", "--allow-empty", patch_file],
                    check=True,
                    cwd=feedstock_dir,
                )
                subprocess.run(
                    ["git", "add", "-f", "."],
                    cwd=feedstock_dir,
                    check=True,
                )

            if task_results["commit_message"] is not None:
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "-m",
                        task_results["commit_message"],
                        "--allow-empty",
                    ],
                    cwd=feedstock_dir,
                    check=True,
                )

        # now do any comments and/or pushes
        if task == "rerender":
            comment_push_error = _push_changes(
                action="rerender",
                action_error=task_results["rerender_error"],
                info_message=task_results["info_message"],
                changed=task_results["changed"],
                git_repo=git_repo,
                pr=pr,
                pr_branch=pr_branch,
                pr_owner=pr_owner,
                pr_repo=pr_repo,
                repo_name=full_repo_name,
                close_pr_if_no_changes_or_errors=False,
            )
            status = "success" if not comment_push_error else "failure"
            target_url = (
                f"https://github.com/conda-forge/conda-forge-webservices/"
                f"actions/runs/{os.environ['GITHUB_RUN_ID']}"
            )
            set_rerender_pr_status(
                gh_repo,
                int(pr_number),
                status,
                target_url=target_url,
                sha=sha_for_status,
            )

            # if the pr was made by the bot, mark it as ready for review
            if (
                (not comment_push_error)
                and pr.title == "MNT: rerender"
                and pr.user.login == "conda-forge-admin"
            ):
                mark_pr_as_ready_for_review(pr)

            if comment_push_error:
                LOGGER.error(
                    f"Error in rerender for {full_repo_name}#{pr_number}! "
                    "Check the workflow logs of the `run task` job for more details!",
                )
                sys.exit(1)

        elif task == "version_update":
            if (
                (not task_results["version_error"])
                and task_results["version_changed"]
                and task_results["new_version"]
            ):
                LOGGER.info(
                    "Updating PR title for %s#%s with version=%s",
                    full_repo_name,
                    pr_number,
                    task_results["new_version"],
                )
                _, pr_title_error = update_pr_title(
                    full_repo_name, int(pr_number), task_results["new_version"]
                )

            if task_results["version_error"]:
                action_error = True
            else:
                if task_results["version_changed"]:
                    # if there is no version error and the version changed
                    # then we can report if rerendering failed
                    action_error = task_results["rerender_error"]
                else:
                    # if the version did not change, we can ignore the rerendering
                    # error if any
                    action_error = False

            comment_push_error = _push_changes(
                action="update the version and rerender",
                action_error=action_error,
                info_message=task_results["info_message"],
                changed=task_results["version_changed"],
                git_repo=git_repo,
                pr=pr,
                pr_branch=pr_branch,
                pr_owner=pr_owner,
                pr_repo=pr_repo,
                repo_name=full_repo_name,
                close_pr_if_no_changes_or_errors=True,
            )

            # we always do this for versions
            if not comment_push_error:
                mark_pr_as_ready_for_review(pr)

            if pr_title_error or comment_push_error:
                LOGGER.error(
                    f"Error in version update for "
                    f"{full_repo_name}#{pr_number}: {pr_title_error=} "
                    f"{comment_push_error=}. "
                    "Check the workflow logs of the `run task` job for more details!",
                )
                sys.exit(1)
        elif task == "lint":
            if (
                task_results["lints"] is not None
                and task_results["hints"] is not None
                and task_results["errors"] is not None
            ):
                recipes_to_lint, _ = get_recipes_for_linting(
                    gh, gh_repo, pr.number, task_results["lints"], task_results["hints"]
                )
                if any(
                    task_results["errors"].get(rec, True) for rec in recipes_to_lint
                ):
                    task_results["lint_error"] = True

            if task_results["lint_error"]:
                run_link = get_gha_run_link()
                _message = dedent_with_escaped_continue(
                    f"""
                    Hi! This is the friendly automated conda-forge-linting service.

                    I failed to even lint the recipe, probably because of a \\
                    conda-smithy \\
                    bug :cry:. This likely indicates a problem in your `meta.yaml`, \\
                    though. To get a traceback to help figure out what's going on, \\
                    install conda-smithy and run \\
                    `conda smithy recipe-lint --conda-forge .` from the recipe \\
                    directory. You can also examine the [workflow logs]({run_link}) \\
                    for more detail.
                    """
                )
                _message += (
                    "\n\n<sub>This message was generated by "
                    f"GitHub Actions workflow run [{run_link}]({run_link}). "
                    "Examine the logs at this URL for more detail.</sub>\n"
                )
                msg = make_lint_comment(gh_repo, pr.number, _message)
                status = "bad"
            else:
                msg, status = build_and_make_lint_comment(
                    gh, gh_repo, pr.number, task_results["lints"], task_results["hints"]
                )

            set_pr_status(gh_repo, sha_for_status, status, target_url=msg.html_url)
            print(f"Linter status: {status}")
            print(f"Linter message:\n{msg.body}")

            if task_results["lint_error"]:
                LOGGER.error(
                    f"Error in linting for {full_repo_name}#{pr_number}! "
                    "Check the workflow logs of the `run task` job for more details!"
                )
                sys.exit(1)
        else:
            raise ValueError(f"Task `{task}` is not valid!")


@click.command(name="conda-forge-webservices-automerge")
@click.option("--repo", required=True, type=str)
@click.option("--sha", required=True, type=str)
def main_automerge(repo, sha):
    logging.basicConfig(level=logging.INFO)

    action_desc = f"task `automerge` for conda-forge/{repo}@{sha}"
    print(
        f"::notice title=conda-forge-webservices job information::{action_desc}",
        flush=True,
    )

    found_pr = False
    full_repo_name = f"conda-forge/{repo}"
    _, gh = create_api_sessions()
    gh_repo = gh.get_repo(full_repo_name)
    for pr in gh_repo.get_pulls():
        if pr.head.sha == sha:
            _, gh_for_admin = create_api_sessions_for_admin()
            gh_repo_for_admin = gh_for_admin.get_repo(full_repo_name)
            pr_for_admin = gh_repo_for_admin.get_pull(pr.number)
            automerge_pr(gh_repo, pr, pr_for_admin)
            found_pr = True

    if not found_pr:
        LOGGER.error(f"No PR found for {full_repo_name}@{sha}!")
        print(
            "::warning title=No PR Found for Automerge::"
            f"No PR found for {full_repo_name}@{sha}",
            flush=True,
        )
