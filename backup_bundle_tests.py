# backup_bundle.py: incremental backup of git repositories based on git bundle
# Copyright (C) 2025  Thomas Schaap
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# ruff: noqa: G004, S101, S404, S603, S607, S311  # This is testing code

"""
Tests for backup_bundle.py.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from logging import getLogger
from pathlib import Path
from shutil import copy
from string import ascii_letters
from tempfile import TemporaryDirectory
from time import sleep
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

# Import the module under test using a bit of a hack so we don't need any package infrastructure. This keeps bundle.py
# a plain single-file python script. This is done last to prevent any other files in the current directory from
# interfering with the previous imports, and to isolate the hack from the rest of the imports.
this_path = str(Path().absolute())
sys.path.append(this_path)
from backup_bundle import (  # noqa: E402
    GitCallFailedError,
    MissingRemoteError,
    NoBundlesRestoredError,
    SimpleLockFileNotCreatedError,
    configure_logging,
    get_current_branch,
    is_bare_repo,
    list_references_in_repo,
    simple_lock_file,
)
from backup_bundle import main as _main  # noqa: E402

sys.path.remove(this_path)

DEFAULT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "A U Thor",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "C Ommitter",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
}
"""A default environment for calling git with. This prevents interference from missing git config."""

UTF8 = "utf-8"

log = getLogger(__name__)


# ####################################
# #####                          #####
# #####   GENERIC TEST HELPERS   #####
# #####                          #####
# ####################################

# These helper functions make the tests a lot easier to read and write. Lots of specialized git calls here, but also
# generic testing stuff like always changing to a temporary directory.


@contextmanager
def in_dir(directory: Path) -> Generator[None, None, None]:
    """
    Change the directory to the given path in the context.

    :param dir: The directory to change to.
    """
    current = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(current)


@pytest.fixture(autouse=True)
def in_tmp_path(tmp_path: Path) -> Generator[Path, None, None]:
    """
    Change directory to a temporary path.

    :param tmp_path: A temporary directory provided by pytest
    :yields: The absolute temporary path.
    """
    with in_dir(tmp_path):
        yield tmp_path


@pytest.fixture(scope="session", autouse=True)
def configure_bundle_logging() -> None:
    """
    Configure the logging for bundle.

    The logger for bundle is only created after it has been configured, which is after main has been called. Before that
    time, however, that logger is indirectly called from the tests. Provide early configuration to fix that.
    """
    configure_logging(
        argparse.Namespace(
            verbose=True,
            log_config_file=None,
            log_config=None,
        )
    )


# ###################################
# #####                         #####
# #####   GIT RELATED HELPERS   #####
# #####                         #####
# ###################################

# These helper functions make the tests a lot easier to read and write. Also includes a reimplementation of `call_git`
# and friends, to pass an environment (in particular for `git commit`).


def backup_bundle_main(*args: str | Path) -> None:
    """
    Call bundle.py main, with all arguments converted to strings.

    :param args: The parameters to pass to main.
    """
    _main([*[str(arg) for arg in args], "-v"])


def _call_git(arguments: list[str], *, cwd: Path) -> list[str]:
    """
    Run a git process and return it's output. Internal helper without error handling.

    :param arguments: The arguments to call git with (e.g. `["status"]` to call `git status`)
    :param cwd: The working directory from which to call git.
    """
    log.debug(f"Calling: git {' '.join(arguments)}")
    process = subprocess.run(
        ["git", *arguments], cwd=str(cwd), capture_output=True, check=True, text=True, env=DEFAULT_ENVIRONMENT
    )
    return process.stdout.splitlines()


def call_git(arguments: list[str], *, cwd: Path) -> list[str]:
    """
    Run a git process and return it's output.

    :param arguments: The arguments to call git with (e.g. `["status"]` to call `git status`)
    :param cwd: The working directory from which to call git.
    """
    try:
        return _call_git(arguments, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        log.debug(f"Call failed. stdout={exc.stdout}, stderr={exc.stderr}")
        raise


def try_call_git(arguments: list[str], *, cwd: Path) -> list[str]:
    """
    Run a git process and return it's output, or an empty list if the command fails.

    Use this call for commands that may fail (e.g. rev-parse for a revision that may or may not resolve).

    :param arguments: The arguments to call git with (e.g. `["status"]` to call `git status`)
    :param cwd: The working directory from which to call git.
    """
    try:
        return _call_git(arguments, cwd=cwd)
    except subprocess.CalledProcessError:
        return []


_main_branch_no_really_call_the_function_instead: str | None = None
"""
The name of the main branch to use in test repositories.

DO NOT USE! Call `main_branch()`, instead.
"""


def main_branch() -> str:
    """
    The name of the main branch to use in test repositories.

    Do not call before the session has been initialized! In particular, if you need this in parameterizations you should
    provide indirection, for example by passing in a lambda that call this function, instead.
    """
    assert _main_branch_no_really_call_the_function_instead is not None, (
        "Do not call upon main_branch() before the session has been initialized."
    )
    return _main_branch_no_really_call_the_function_instead


@pytest.fixture(scope="session", autouse=True)
def determine_main_branch() -> str:
    """
    Fixture to determine the main branch of new git repositories.

    This will initialize `main_branch()`.

    :yields: The name of the main branch.
    """
    global _main_branch_no_really_call_the_function_instead  # noqa: PLW0603

    # The only reliable way to figure this out is by creating a new repository.
    with TemporaryDirectory() as tmp:
        branch_name, _ = get_current_branch(create_repo(tmp))

    assert branch_name
    _main_branch_no_really_call_the_function_instead = branch_name
    return branch_name


def assert_repos_equal(repo1: Path, repo2: Path) -> None:
    """
    Assert that two repositories contains the same references and reachable commits for those references.

    Bundle files are not supported.

    :param repo1: The one repo to check.
    :param repo2: The other repo to check.
    """
    assert repo1.resolve() != repo2.resolve(), "You probably meant to compare two *different* repos."
    refs1 = list_references_in_repo(repo1)
    refs2 = list_references_in_repo(repo2)
    assert set(refs1) == set(refs2)
    for ref in refs1:
        assert call_git(["rev-list", ref.ref], cwd=repo1) == call_git(["rev-list", ref.ref], cwd=repo2)


def assert_repos_not_equal(repo1: Path, repo2: Path) -> None:
    """
    Assert that two repositories do not contains the same references and/or reachable commits for those references.

    Bundle files are not supported.

    :param repo1: The one repo to check.
    :param repo2: The other repo to check.
    """
    assert repo1.resolve() != repo2.resolve(), "You probably meant to compare two *different* repos."
    refs1 = list_references_in_repo(repo1)
    refs2 = list_references_in_repo(repo2)
    if set(refs1) != set(refs2):
        return
    for ref in refs1:
        if call_git(["rev-list", ref.ref], cwd=repo1) != call_git(["rev-list", ref.ref], cwd=repo2):
            return
    # Assert the references again. This gives the clearest output.
    assert set(refs1) != set(refs2)


def create_repo(repo: Path | str, *, clone: Path | str | None = None, bare: bool = False, mirror: bool = False) -> Path:
    """
    Create a git repository in repo.

    :param repo: Location of the new repository.
    :param clone: If set, clone from this source.
    :param bare: Create a bare repository.
    :param mirror: Create a mirrored clone.
    :returns: `repo`
    """
    if isinstance(repo, str):
        repo = Path(repo)
    if isinstance(clone, str):
        clone = Path(clone)
    repo.mkdir(parents=True, exist_ok=True)
    options: list[str] = []
    if clone is not None:
        if mirror:
            options = ["--mirror"]
        elif bare:
            options = ["--bare"]
        call_git(["clone", *options, str(clone.absolute()), "."], cwd=repo)
    else:
        assert not mirror, "mirror makes no sense if not cloning"
        if bare:
            options = ["--bare"]
        call_git(["init", *options, "."], cwd=repo)
    return repo


def add_commits(repo: Path, branch: str | None = None, *, count: int = 1, filename: str = "random_file") -> None:
    """
    Create commits on a repo.

    Each commit will overwrite the same file with a random string of the same length.

    :param repo: The repo to commit on.
    :param branch: The branch to create commits on. Defaults to `main_branch()`.
    :param count: The number of commits to add.
    :param filename: The name of the file that will be updated in every commit.
    """
    assert not is_bare_repo(repo), "Can't create commits on bare repo"
    assert count >= 0

    branch = branch or main_branch()

    # Detect an empty repository (= main branch, no commits yet). Otherwise switch to the requested branch.
    if branch != main_branch() or try_call_git(["rev-parse", main_branch()], cwd=repo):
        call_git(["switch", "--no-guess", branch], cwd=repo)

    # Create the commits
    with in_dir(repo):
        file = Path(filename)

        for _ in range(count):
            # Ensure we have some data to commit
            current = file.read_text(encoding=UTF8) if file.exists() else ""
            new = current
            while new == current:
                new = "".join(random.choices(ascii_letters, k=16))
            file.write_text(new, encoding=UTF8)

            # Create the commit
            call_git(["add", str(file)], cwd=Path())
            call_git(["commit", "-m", "Another commit"], cwd=Path())


def create_branch(repo: Path, branch: str, *, commit: str | None = None) -> str:
    """
    Create a new branch on a repository.

    :param repo: The repo to add a branch in.
    :param branch: The name of the branch to create.
    :param commit: Start the new branch from this commit. Defaults to `main_branch()`.
    :returns: `branch`
    """
    commit = commit or main_branch()
    call_git(["branch", branch, commit], cwd=repo)
    return branch


def change_branch(repo: Path, branch: str, *, new_commit: str) -> None:
    """
    Change an existing branch from a repository.

    :param repo: The repo to change the branch in.
    :param branch: The branch to change.
    :param new_commit: The new commit to point the branch to.
    """
    call_git(["branch", "--force", branch, new_commit], cwd=repo)


def delete_branch(repo: Path, branch: str) -> None:
    """
    Change an existing branch from a repository.

    :param repo: The repo to change the branch in.
    :param branch: The branch to change.
    """
    call_git(["switch", "--detach", call_git(["rev-parse", branch], cwd=repo)[0]], cwd=repo)
    call_git(["branch", "--delete", "--force", branch], cwd=repo)


def create_tag(repo: Path, tag: str, commit: str) -> str:
    """
    Create a new tag on a repository.

    :param repo: The repo to add the tag to.
    :param tag: The name of the tag to create.
    :param commit: The commit to tag.
    :returns: `tag`
    """
    call_git(["tag", "--no-sign", tag, commit], cwd=repo)
    return tag


def bundle_verifies(repo: Path, bundle: Path) -> bool:
    """
    Check whether the bundle verifies for a repository.

    :param repo: The repository to check in.
    :param bundle: The bundle file to check.
    :returns: True iff the bundle can be restored according to `git bundle verify`.
    """
    try:
        call_git(["bundle", "verify", str(bundle.absolute())], cwd=repo)
    except subprocess.CalledProcessError:
        return False
    else:
        return True


def list_reference_names_in_repo(repo: Path) -> set[str]:
    """
    List the full reference names of all references in a repository.

    This function can handle bundles.

    :param repo: The repository to list.
    """
    return {ref.ref for ref in list_references_in_repo(repo)}


# ##########################
# #####                #####
# #####   UNIT TESTS   #####
# #####                #####
# ##########################

# Some functions of bundle aren't covered with full certainty by the rest of the tests. Their corner cases are covered
# in these unit tests.


def test_unit_get_current_branch() -> None:
    """
    Verify that get_current_branch returns the expected results.
    """
    repo = create_repo("repo")
    bare_repo = create_repo("bare", clone=repo, bare=True, mirror=True)

    # A new repository returns only the name of the main branch
    assert (main_branch(), None) == get_current_branch(repo)
    assert (main_branch(), None) == get_current_branch(bare_repo)

    add_commits(repo)
    call_git(["remote", "update"], cwd=bare_repo)

    # A normal checkout returns the name of the branch and the checked out commit
    branch, ref = get_current_branch(repo)
    assert branch == main_branch()
    assert ref is not None
    assert ref.ref == f"refs/heads/{main_branch()}"

    branch, ref = get_current_branch(bare_repo)
    assert branch == main_branch()
    assert ref is not None
    assert ref.ref == f"refs/heads/{main_branch()}"

    # Switch to a detached head (the update-ref is the plumbing way to do the same in a bare repo)
    call_git(["switch", "--detach"], cwd=repo)
    call_git(["update-ref", "--no-deref", "HEAD", ref.hash], cwd=bare_repo)

    # A detached head returns only an empty string
    assert get_current_branch(repo) == ("", None)
    assert get_current_branch(bare_repo) == ("", None)


def test_unit_simple_lock_file() -> None:
    """
    Verify that the simple_lock_file context manager works.
    """
    lock_file = Path("the_lock")

    # Basic lock scenario
    assert not lock_file.exists()

    with simple_lock_file(lock_file):
        assert lock_file.exists()

    assert not lock_file.exists()

    # File already exists (lock fails)
    lock_file.write_text("", encoding=UTF8)

    assert lock_file.exists()

    with pytest.raises(SimpleLockFileNotCreatedError):  # noqa: SIM117  # This is more obvious to the reader
        with simple_lock_file(lock_file):
            pytest.fail("Should not be reached")

    assert lock_file.exists()

    lock_file.unlink()

    # Lock-in-lock scenarion (inner lock fails)
    assert not lock_file.exists()

    with simple_lock_file(lock_file):
        with pytest.raises(SimpleLockFileNotCreatedError):  # noqa: SIM117  # This is more obvious to the reader
            with simple_lock_file(lock_file):
                pytest.fail("Should not be reached")
        assert lock_file.exists()

    assert not lock_file.exists()


# ############################
# #####                  #####
# #####   BACKUP TESTS   #####
# #####                  #####
# ############################

# These tests focus on the creation of backups. Restores are mainly done to verify the correctness of the backup.


def test_create_full() -> None:
    """
    Verify that a basic full backup and restore succeeds.

    Also verifies that backing up from a normal repo works.
    Also verifies that backing up to a new bundle works.
    """
    bundle = Path("bundle.bundle")
    metadata = Path("metadata.json")
    target = Path("target")

    assert not bundle.exists()

    origin = create_repo("origin", bare=False)
    add_commits(origin, count=3)
    b1 = create_branch(origin, "b1", commit=f"{main_branch()}~2")
    add_commits(origin, b1, count=4)
    b2 = create_branch(origin, "b2")
    add_commits(origin, b2, count=2)
    create_tag(origin, "tag", "b1~1")

    # Create the full backup
    backup_bundle_main("create", origin, bundle, "--metadata", metadata)

    # Backup was created
    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--force")

    # Full restore succeeded
    assert_repos_equal(origin, target)


def test_create_incremental() -> None:
    """
    Verify that an incremental backup and restore succeeds, and is in fact incremental.

    Also verifies that incremental backups against a previous bundle in a different location work.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    moved_bundle = Path("previous_bundle1.bundle")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=2)

    target = create_repo("target", bare=True)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Move the original backup bundle, so we are certain it can't be used as reference for the incremental. The previous
    # bundle location should be used.
    bundles[0] = bundles[0].rename(moved_bundle)

    add_commits(origin, count=1)

    # Create the incremental backup
    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # The second bundle is an incremental backup (based on `git bundle verify`'s verdict)
    assert bundle_verifies(target, bundles[0])
    assert not bundle_verifies(target, bundles[1])

    backup_bundle_main("restore", target, bundles[0])

    # Restoring the first bundle is not a full restore, but sufficient to restore the incremental bundle afterwards
    assert_repos_not_equal(origin, target)
    assert bundle_verifies(target, bundles[0])
    assert bundle_verifies(target, bundles[1])

    backup_bundle_main("restore", target, bundles[1], "--force")

    # Incremental restore succeeded
    assert_repos_equal(origin, target)


def test_create_includes_unchanged_references() -> None:
    """
    Verify that references that have no changes at all are still included in an incremental backup.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=2)
    b1 = create_branch(origin, "b1", commit=f"{main_branch()}~1")
    add_commits(origin, b1, count=10)
    create_branch(origin, "b2", commit=f"{b1}~7")

    target = create_repo("target", bare=True)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Note that the new commits are made on the main branch, so b1, b2 and le_tag are not reachable from them
    add_commits(origin, count=2)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # All references are mentioned in both bundles
    assert list_reference_names_in_repo(bundles[0]) == list_reference_names_in_repo(bundles[1])

    backup_bundle_main("restore", target, bundles[0])
    backup_bundle_main("restore", target, bundles[1], "--force")

    # Incremental restore succeeds and keeps all references
    assert_repos_equal(origin, target)


TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS: Final[int] = 8
"""
The total number of commits used for test_create_reference_inclusions_and_exclusions.

This must be an even number. It does not include the first commit to ensure that the incremental backup in the test
can't accidentally be a full backup.

Please be warned that increasing this number greatly increases the number of tests.
"""


def _generate_distances() -> Generator[tuple[int, int, int], None, None]:
    """
    Generate the distances for test_create_reference_inclusions_and_exclusions.
    """
    for distance1 in range(TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS):
        for distance2 in range(TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS - distance1):
            for distance3 in range(TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS - (distance1 + distance2)):
                yield (distance1, distance2, distance3)


@pytest.mark.parametrize(
    ("distance1", "distance2", "distance3"),
    list(_generate_distances()),
    # Because it's impractical to find all corner cases, a whole string of tests is attempted. For this, git repos with
    # a history like below are created, with the branches' locations parameterised by their distance from the previous
    # one (or the main branch):
    #
    # commit 8B8B (HEAD, main)     --\
    # commit 7777                    |-> distance1 (= 2)
    # commit 6666 (b1)             --/                      --\
    # commit 5555                                             |-> distance2 (= 3)
    # commit 4F4F                                             |
    # commit 3333 (b2)             --\                      --/
    # commit 2222 (b3)             --/-> distance3 (= 1)
    # commit 1111
    # commit 0000
    #
    # First commits 1111 - 4F4F are created, and any branches that would end up there. A first backup is created there.
    # Then the rest of the commits and branches is created, and an incremental backup is created. By parameterizing the
    # location of the branches, they might (among other unforeseen corner cases):
    # - overlap
    # - be on consecutive commits
    # - be in the first full backup
    # - be in the incremental backup
    # - be on or around the border between the two backups
    #
    # The number of commits is chosen such that two branches fit entirely inside the first or the second backup, without
    # them being on consecutive commits and without interacting with the commit where the initial backup is made (4F4F).
    # On top of that one additional commit it added in the beginning (0000). This ensures that, no matter where the
    # branches end up, the second bundle can't contain all commits in the repo. This way we can verify that it is indeed
    # an incremental backup by attempting `git bundle verify` against an empty repo (which should hence fail).
)
def test_create_reference_inclusions_and_exclusions(distance1: int, distance2: int, distance3: int) -> None:
    """
    Verify that the inclusion/exclusion selection correctly includes all references, no matter their relative distances.
    """

    def add_branch(name: str, current_commit_count: int, branch_distance: int, *, already_added: bool) -> bool:
        """
        Add a branch to the origin repository, if its intended commit exists.

        :param name: Name of the branch to add.
        :param current_commit_count: Number of commits that has so far been added to the main branch.
        :param branch_distance: Intended distance from the final main branch (which will have 8 commits) for the commit
                                to branch off (i.e. the branch will start at `main_branch~branch_distance`).
        :param added: If the branch was previously added.
        :returns: Whether the branch was (previously) added.
        """
        if already_added:
            return True
        current_distance = branch_distance - (TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS - current_commit_count)
        if current_distance < 0:
            return False
        create_branch(origin, name, commit=f"{main_branch()}~{current_distance}")
        return True

    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")
    target = create_repo("target", bare=True)

    origin = create_repo("origin")

    # Add the first commits and the branches that end up there. Add one additional commit to make sure the incremental
    # bundle can't verify against an empty repo.
    half_of_total_commits = TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS // 2
    add_commits(origin, count=1 + half_of_total_commits)
    b1 = add_branch("b1", half_of_total_commits, distance1, already_added=False)
    b2 = add_branch("b2", half_of_total_commits, distance1 + distance2, already_added=False)
    b3 = add_branch("b3", half_of_total_commits, distance1 + distance2 + distance3, already_added=False)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Add the last commits and the rest of the branches
    add_commits(origin, count=half_of_total_commits)
    b1 = add_branch("b1", TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS, distance1, already_added=b1)
    b2 = add_branch("b2", TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS, distance1 + distance2, already_added=b2)
    b3 = add_branch("b3", TOTAL_COMMITS_FOR_INCLUSIONS_EXCLUSIONS, distance1 + distance2 + distance3, already_added=b3)

    # Each branch should now have been added
    assert b1
    assert b2
    assert b3

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # The incremental backup contains all references, and is not a full backup
    assert set(list_references_in_repo(origin)) == set(list_references_in_repo(bundles[1]))
    assert not bundle_verifies(target, bundles[1])

    backup_bundle_main("restore", target, bundles[0])
    backup_bundle_main("restore", target, bundles[1], "--force")

    # Restoring the backups succeeded
    assert_repos_equal(origin, target)


@pytest.mark.parametrize(
    "commit",
    [
        # Lambdas, because main_branch() only becomes available later on
        main_branch,
        lambda: f"{main_branch()}~1",
        lambda: f"{main_branch()}~2",
        lambda: f"{main_branch()}~3",
    ],
)
def test_create_new_tag_in_incremental_backup(commit: Callable[[], str]) -> None:
    """
    Verify that a new tag shows up in an incremental backup.

    :param commit: The commit to create the tag on.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")
    metadata = Path("metadata.json")

    origin = create_repo("origin")
    add_commits(origin, count=2)

    target = create_repo("target", bare=True)

    backup_bundle_main(
        "create", origin, bundles[0], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    # Add a few commits and the new tag
    add_commits(origin, count=2)
    create_tag(origin, "tagged", commit=commit())

    # Create the incremental backup
    backup_bundle_main(
        "create", origin, bundles[1], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    backup_bundle_main("restore", target, bundles[0])
    backup_bundle_main("restore", target, bundles[1], "--force")

    # Restoring the backups succeeded
    assert_repos_equal(origin, target)


def test_create_requires_actual_git_repo() -> None:
    """
    Verify that attempting to create a backup from something that is not a git repo fails.
    """
    origin = Path("origin")
    origin.mkdir()
    (origin / "some_file").write_text("bla")

    with pytest.raises(GitCallFailedError):
        backup_bundle_main("create", origin, "bundle.bundle")


def test_create_from_empty_directory_requires_remote() -> None:
    """
    Verify that attempting to create a backup from an empty directory without specifying a remote fails.
    """
    origin = Path("origin")
    origin.mkdir()

    with pytest.raises(MissingRemoteError):
        backup_bundle_main("create", origin, "bundle.bundle")


def test_create_from_empty_directory() -> None:
    """
    Verify that attempting to create a backup from an empty directory works.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    source = create_repo("source")
    add_commits(source)

    origin = Path("origin")
    origin.mkdir()

    backup_bundle_main("create", origin, bundle, "--remote", source)

    # Creating the backup has caused a normal clone in the origin repo
    assert_repos_equal(source, origin)
    assert not is_bare_repo(origin)

    backup_bundle_main("restore", target, bundle)

    # Restoring the backups succeeded
    assert_repos_equal(source, target)


def test_create_from_non_existent_directory() -> None:
    """
    Verify that attempting to create a backup from a directory that does not exist still works.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    source = create_repo("source")
    add_commits(source)

    origin = Path("origin")

    backup_bundle_main("create", origin, bundle, "--remote", source)

    # Creating the backup has caused a normal clone in the origin repo
    assert_repos_equal(source, origin)
    assert not is_bare_repo(origin)

    backup_bundle_main("restore", target, bundle)

    # Restoring the backups succeeded
    assert_repos_equal(source, target)


def test_create_from_empty_directory_mirror_mode() -> None:
    """
    Verify that creating a backup from a non-existing directories works in mirror mode.

    Also verifies that backing up from a bare repository works.
    """
    origin = Path("origin")
    bundle = Path("bundle.bundle")
    target = Path("target")

    source = create_repo("source")
    add_commits(source)

    backup_bundle_main("create", origin, bundle, "--remote", source, "--mirror")

    # Creating the backup has caused a mirrored clone in the origin repo
    assert_repos_equal(source, origin)
    assert is_bare_repo(origin)

    backup_bundle_main("restore", target, bundle)

    # Restoring the backups succeeded
    assert_repos_equal(source, target)


def test_create_in_mirror_mode_triggers_update_first() -> None:
    """
    Verify that creating backups in mirror mode first triggers an update.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    source = create_repo("source")
    add_commits(source)

    origin = create_repo("origin", clone=source, mirror=True)

    target = create_repo("target", bare=True)

    # Add some more commits to the source and create a full backup of origin
    add_commits(source)
    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle, "--mirror")

    backup_bundle_main("restore", target, bundles[0])

    # Restoring will show the new commits to source to have been included
    assert_repos_equal(source, target)

    # Add some more commits to the source and create an incremental backup
    add_commits(source)
    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle, "--mirror")

    backup_bundle_main("restore", target, bundles[1], "--force")

    # Restoring will show the new commits to source to have been included
    assert_repos_equal(source, target)


@pytest.mark.parametrize(
    "create",
    [
        False,
        True,
    ],
)
def test_create_to_timestamped_bundle(*, create: bool) -> None:
    """
    Verify that creating timestamped backups works.

    :param create: Create files to make sure the non-timestamped bundle can't be written.
    """
    bundle_dir = Path("bundle_dir")
    bundle = bundle_dir / "bundle.bundle"
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    target = create_repo("target", bare=True)

    if create:
        # Make it "impossible" to try and write the original file.
        # This verifies that it's not a write-then-move implementation.
        bundle.mkdir(parents=True)
        (bundle / "some_file").write_text("bla")

    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle, "--timestamp")

    # Bundle file should not have been written
    if create:
        assert bundle.exists()
        assert not bundle.is_file()
    else:
        assert not bundle.exists()

    # Exactly one file was written
    assert len([found_bundle for found_bundle in bundle_dir.iterdir() if found_bundle.is_file()]) == 1
    # Bundle files have the timestamp injected as the second-to-last suffix
    assert all(
        Path(found_bundle.stem).stem == bundle.stem and found_bundle.suffix == bundle.suffix
        for found_bundle in bundle_dir.iterdir()
        if found_bundle.is_file()
    )

    # Try again in one second, with an incremental backup
    sleep(1)
    add_commits(origin)
    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle, "--timestamp")

    # Exactly two files were written
    assert len([found_bundle for found_bundle in bundle_dir.iterdir() if found_bundle.is_file()]) == 2  # noqa: PLR2004
    # Bundle files have the timestamp injected as the second-to-last suffix
    assert all(
        Path(found_bundle.stem).stem == bundle.stem and found_bundle.suffix == bundle.suffix
        for found_bundle in bundle_dir.iterdir()
        if found_bundle.is_file()
    )

    # Restoring the timestamped files should work as normal
    backup_bundle_main("restore", target, bundle_dir)
    assert_repos_equal(origin, target)


def test_create_with_bundle_equals_previous_bundle_location() -> None:
    """
    Verify that a backup and incremental backup with bundle==previous-bundle-location work correctly.

    Also verifies that backing up to an existing bundle file works.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", bundle)

    backup_bundle_main("restore", target, bundle)

    # Backup and restore should have succeeded
    assert_repos_equal(origin, target)

    add_commits(origin)

    assert bundle.exists()

    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", bundle)

    backup_bundle_main("restore", target, bundle, "--force")

    # Incremental backup and restore should have succeeded
    assert_repos_equal(origin, target)


def test_create_branch_on_first_commit() -> None:
    """
    Verify that creating a backup with a branch on the first ever commit in the repository works correctly.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin, count=1)  # Explicitly only one commit. We need a branch right at this root commit.
    b1 = create_branch(origin, "b1")
    add_commits(origin, b1, count=1)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle)

    # Full restore succeeded
    assert_repos_equal(origin, target)

    # The main branch is at the first-ever commit in the target repository (= parent commit does not exist)
    assert not try_call_git(["rev-parse", f"{main_branch()}~1"], cwd=target)


def test_create_only_includes_tags_with_metadata_option() -> None:
    """
    Verify that backing up tags only works if a metadata file is used.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle"), Path("bundle3.bundle")]
    previous_bundle = Path("previous.bundle")
    previous_bundle_backup = Path("previous.backup")
    metadata = Path("metadata.json")

    origin = create_repo("origin")
    add_commits(origin, count=3)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Keep a copy of the previous bundle, so we can do the same incremental backup twice
    copy(previous_bundle, previous_bundle_backup)

    add_commits(origin, count=2)
    tag = create_tag(origin, "le_tag", f"{main_branch()}~1")

    backup_bundle_main(
        "create", origin, bundles[1], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    # Tag is included when metadata is used
    assert f"refs/tags/{tag}" in list_reference_names_in_repo(bundles[1])

    # Undo the incremental backup
    bundles[1].unlink()
    previous_bundle.unlink()
    metadata.unlink()
    copy(previous_bundle_backup, previous_bundle)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # Tag is not included when metadata is not used
    assert f"refs/tags/{tag}" not in list_reference_names_in_repo(bundles[1])

    add_commits(origin, count=2)

    backup_bundle_main(
        "create", origin, bundles[2], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    # The old tag is included now that metadata is used, even though it's not in the commits being backed up
    assert f"refs/tags/{tag}" in list_reference_names_in_repo(bundles[2])


def test_create_tags_incremental() -> None:
    """
    Verify that backing up tags works incrementally.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")
    metadata = Path("metadata.json")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin, count=3)
    tag1 = create_tag(origin, "le_tag", f"{main_branch()}~1")

    backup_bundle_main(
        "create", origin, bundles[0], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    add_commits(origin, count=2)
    tag2 = create_tag(origin, "other_tag", f"{main_branch()}~1")

    backup_bundle_main(
        "create", origin, bundles[1], "--previous-bundle-location", previous_bundle, "--metadata", metadata
    )

    # First tag is only in the first bundle, second one is in the second bundle
    assert f"refs/tags/{tag1}" in list_reference_names_in_repo(bundles[0])
    assert f"refs/tags/{tag2}" not in list_reference_names_in_repo(bundles[0])
    assert f"refs/tags/{tag1}" not in list_reference_names_in_repo(bundles[1])
    assert f"refs/tags/{tag2}" in list_reference_names_in_repo(bundles[1])

    backup_bundle_main("restore", target, bundles[0], "--force")
    backup_bundle_main("restore", target, bundles[1], "--force")

    # Full restore of backup succeeded
    assert_repos_equal(origin, target)


def test_create_existing_repo_with_remote() -> None:
    """
    Verify that passing a (different) remote when backing up an existing repository is inconsequential.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin, count=2)

    bad_source = create_repo("bad_source")
    add_commits(bad_source, count=2)

    # Baseline test: the two repositories are different
    assert_repos_not_equal(origin, bad_source)

    backup_bundle_main("create", origin, bundle, "--remote", bad_source)

    backup_bundle_main("restore", target, bundle)

    # The origin repo has not been changed and has been backed up as it was (i.e. --remote was ignored)
    assert_repos_not_equal(origin, bad_source)
    assert_repos_equal(origin, target)


def test_create_does_not_write_previous_bundle_upon_failure() -> None:
    """
    Verify that when an (incremental) backup fails, the previous bundle is not written.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")
    previous_bundle_copy = Path("previous_copy.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    # Make bundle unwriteable as a file
    bundle.mkdir()

    with pytest.raises(GitCallFailedError):
        backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle)

    # Previous bundle has not been written
    assert not previous_bundle.exists()

    # Restore the 'fix' and create a full backup, so we can also try on incremental
    bundle.rmdir()
    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle)
    backup_bundle_main("restore", target, bundle)

    # Make bundle unwriteable as a file, and keep a copy of the previous bundle for reference
    bundle.unlink()
    bundle.mkdir()
    copy(previous_bundle, previous_bundle_copy)

    with pytest.raises(GitCallFailedError):
        backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle)

    # Previous bundle has not been written to
    assert previous_bundle.read_bytes() == previous_bundle_copy.read_bytes()


def test_create_with_no_new_commits() -> None:
    """
    Verify that creating a backup when no new commits exist is allowed.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    # Creation of a new backup without new commits is okay
    backup_bundle_main("create", origin, bundle, "--previous-bundle-location", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    assert_repos_equal(origin, target)


def test_create_creates_bundle_parent_directories() -> None:
    """
    Verify that when the bundle to be created is in a directory that does not yet exist, that directory is created.
    """
    bundle = Path("these/directories/do/not/exist") / "bundle.bundle"

    origin = create_repo("origin")
    add_commits(origin)

    assert not bundle.parent.exists()

    backup_bundle_main("create", origin, bundle)

    assert bundle.exists()


def test_create_skip_unchanged_without_anything_new() -> None:
    """
    Verify that no bundle is created when there is nothing new and --skip-unchanged is passed.

    Also verifies that a bundle is created with --skip-unchanged if there is no stored bundle, yet.

    Also verifies that a metadata file is not written if a bundle is not created.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")
    metadata = Path("metadata.json")

    origin = create_repo("origin")
    add_commits(origin)

    # A bundle can be created with --skip-unchanged if there is no stored bundle, yet
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    backup_bundle_main("restore", target, bundle, "--delete-files")
    assert_repos_equal(origin, target)

    # No bundle will be created with no changed
    assert not bundle.exists()
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert not bundle.exists()

    # No bundle will be created with no changed, no metadata will be written since no bundle is written
    backup_bundle_main(
        "create", origin, bundle, "--previous-bundle", previous_bundle, "--metadata", metadata, "--skip-unchanged"
    )

    assert not bundle.exists()
    assert not metadata.exists()


def test_create_skip_unchanged_new_commit() -> None:
    """
    Verify that a bundle is created when there is a new commit and --skip-unchanged is passed.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    add_commits(origin)

    # A bundle will be created if there is a new commit
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--delete-files")
    assert_repos_equal(origin, target)


@pytest.mark.parametrize("commit", ["b", "b~1"])
def test_create_skip_unchanged_new_tag(commit: str) -> None:
    """
    Verify that a bundle is created when there is a new tag and --skip-unchanged is passed.

    :param commit: Reference to the commit on which to put the tag.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")
    metadata = Path("metadata.json")

    origin = create_repo("origin")
    add_commits(origin, count=2)
    create_branch(origin, "b")

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--metadata", metadata)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    create_tag(origin, "a_tag", commit)

    # A bundle will be created if there is a new tag
    backup_bundle_main(
        "create", origin, bundle, "--previous-bundle", previous_bundle, "--metadata", metadata, "--skip-unchanged"
    )

    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--delete-files", "--force")
    assert_repos_equal(origin, target)


@pytest.mark.parametrize("commit", ["b", "b~1"])
def test_create_skip_unchanged_new_branch(commit: str) -> None:
    """
    Verify that a bundle is created when there is a new branch and --skip-unchanged is passed.

    :param commit: The commit on which to create the new branch.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=2)
    create_branch(origin, "b")

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    create_branch(origin, "b2", commit=commit)

    # A bundle will be created if there is a new branch
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--delete-files", "--force")
    assert_repos_equal(origin, target)


@pytest.mark.parametrize("commit", ["b", "b~1", "b~3"])
def test_create_skip_unchanged_changed_branch(commit: str) -> None:
    """
    Verify that a bundle is created when a branch was changed and --skip-unchanged is passed.

    :param commit: The commit to which to change the branch.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=4)
    create_branch(origin, "b")
    create_branch(origin, "b2", commit="b~2")

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    change_branch(origin, "b2", new_commit=commit)

    # A bundle will be created if a branch is changed
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--delete-files", "--force")
    assert_repos_equal(origin, target)


@pytest.mark.parametrize("commit", ["b", "b~1", "b~2"])
def test_create_skip_unchanged_removed_branch(commit: str) -> None:
    """
    Verify that a bundle is created when a branch was removed and --skip-unchanged is passed.

    :param str: The commit on which the removed branch was placed.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)
    create_branch(origin, "b")
    create_branch(origin, "b2", commit="b~1")
    create_branch(origin, "b3", commit=commit)

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    delete_branch(origin, "b3")

    # A bundle will be created if a branch is removed
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert bundle.exists()

    backup_bundle_main("restore", target, bundle, "--delete-files", "--force", "--prune")
    assert_repos_equal(origin, target)


def test_create_skip_unchanged_new_tag_but_no_tags_included() -> None:
    """
    Verify that no bundle is created when there is a new tag and --skip-unchanged is passed, but tags are not included.

    Also verifies that a bundle will be created when there is a tag and tags are included for the first time, and that
    metadata will then also be written.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    previous_bundle = Path("previous.bundle")
    metadata = Path("metadata.json")

    origin = create_repo("origin")
    add_commits(origin, count=3)

    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    create_tag(origin, "a_tag", commit=f"{main_branch()}~1")

    # No bundle is created if there is a new tag and --skip-unchanged is passed, but tags are not included
    backup_bundle_main("create", origin, bundle, "--previous-bundle", previous_bundle, "--skip-unchanged")

    assert not bundle.exists()

    # A bundle is created after all if tags are then included, and metadata is then written, as well
    backup_bundle_main(
        "create", origin, bundle, "--previous-bundle", previous_bundle, "--metadata", metadata, "--skip-unchanged"
    )

    backup_bundle_main("restore", target, bundle, "--delete-files", "--force")
    assert_repos_equal(origin, target)
    assert metadata.exists()


# #############################
# #####                   #####
# #####   RESTORE TESTS   #####
# #####                   #####
# #############################

# These tests focus on the restoration of backups. Backups are mainly created to set up the scenarios.


@pytest.mark.parametrize("bare", [True, False])
def test_restore_works_on_existing_repo(*, bare: bool) -> None:
    """
    Verify that restoring into an existing repository functions.

    Also verifies restoring from a single bundle file.

    Also verifies updating the currently checked out branch works on a clean working tree without the need for --force.

    :param bare: Whether to restore to a bare repository.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    target = create_repo("target", bare=bare)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    assert bundles[0].is_file()

    # Restore of the initial bundle always succeeds
    backup_bundle_main("restore", target, bundles[0])

    # Working tree is clean
    if not bare:
        assert not call_git(["status", "--porcelain=1"], cwd=target)

    # The incremental update succeeds, without any strategies
    backup_bundle_main("restore", target, bundles[1])

    # HEAD in the target repo is the updated tip of the main branch
    assert get_current_branch(target) == get_current_branch(origin)

    # Backups have been restored successfully
    assert_repos_equal(origin, target)


def test_restore_non_existing_bundle_fails() -> None:
    """
    Verify that restoring a non-existing bundle fails and leaves the target repository untouched.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    non_existing = Path("not.a.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle)
    backup_bundle_main("restore", target, bundle)

    # Restore non-existing bundle file
    assert not non_existing.exists()
    with pytest.raises(GitCallFailedError):
        backup_bundle_main("restore", target, non_existing)

    # Repository has not been touched
    assert_repos_equal(origin, target)


@pytest.mark.parametrize("bare", [True, False])
def test_restore_to_empty_directory(*, bare: bool) -> None:
    """
    Verify that restoring to an empty directory works.

    :param bare: Whether to use --bare when restoring.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)

    target.mkdir()

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle, *(["--bare"] if bare else []))

    # Restoring the backups succeeded and resulted in a git repo (bare, if requested)
    assert_repos_equal(origin, target)
    assert is_bare_repo(target) == bare


@pytest.mark.parametrize("bare", [True, False])
def test_restore_to_non_existent_directory(*, bare: bool) -> None:
    """
    Verify that attempting to restore to a directory that does not exist still works.

    :param bare: Whether to use --bare when restoring.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle, *(["--bare"] if bare else []))

    # Restoring the backups succeeded and resulted in a git repo (bare, if requested)
    assert_repos_equal(origin, target)
    assert is_bare_repo(target) == bare


@pytest.mark.parametrize(
    "order",
    [
        (0, 1, 2),
        (0, 2, 1),
        (1, 0, 2),
        (1, 2, 0),
        (2, 0, 1),
        (2, 1, 0),
    ],
)
def test_restore_bundle_directory(order: tuple[int, int, int]) -> None:
    """
    Verify that restoring a directory of bundles succeeds despite the order of the bundles.

    :param order: The order in which the bundles are stored in the directory.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    bundle_dir = Path("bundles")

    origin = create_repo("origin")

    bundle_dir.mkdir()

    # Create the bundles and store them in the parameterized order
    for bundle_order in order:
        add_commits(origin)

        backup_bundle_main("create", origin, bundle)
        copy(bundle, bundle_dir / f"bundle{bundle_order}.bundle")

    # Just one restore call for the entire directory
    backup_bundle_main("restore", target, bundle_dir)

    # Restoring from the directory succeeds
    assert_repos_equal(origin, target)


@pytest.mark.parametrize(
    "delete",
    [
        False,
        True,
    ],
)
def test_restore_bundle_directory_leftovers(*, delete: bool) -> None:
    """
    Verify that restoring a directory of bundles succeeds even if there are leftovers, that is, bundles that could not
    be restored (yet).

    Also verifies that unrestorable bundles are not removed by --delete-files.

    :param delete: Whether --delete-files is used during restore.
    """
    bundle = Path("bundle.bundle")
    forgottenbundle = Path("forgotten.bundle")
    target = Path("target")
    bundle_dir = Path("bundles")
    leftover = bundle_dir / "leftover.bundle"

    origin = create_repo("origin")

    bundle_dir.mkdir()

    # Create a few bundles that will be restored
    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, bundle_dir / "bundle0.bundle")

    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, bundle_dir / "bundle1.bundle")

    # Snapshot the state of the origin repository for later comparison
    snapshot = create_repo("intermediate", clone=origin)

    # Create a bundle but "forget" to store it in the directory with bundles
    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, forgottenbundle)

    # Create another bundle to be restored; this will be a leftover (restoring it requires the "forgotten" bundle first)
    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, leftover)

    delete_option = ["--delete-files"] if delete else []
    backup_bundle_main("restore", target, bundle_dir, "--force", *delete_option)

    # Restoring only succeeded until the snapshot (i.e. the first two bundles were restored)
    assert_repos_equal(snapshot, target)
    assert leftover.exists()

    # Retry after adding the forgotten bundle to the directory of bundles
    forgottenbundle.rename(bundle_dir / "forgotten.bundle")
    backup_bundle_main("restore", target, bundle_dir, "--force", *delete_option)

    # Restoring should have fully succeeded
    assert_repos_equal(origin, target)
    if delete:
        assert not leftover.exists()


@pytest.mark.parametrize("extra_commits", [True, False])
def test_restore_outdated_bundle_without_force(*, extra_commits: bool) -> None:
    """
    Verify that restoring an outdated bundle does not fail, even without --force.

    :param extra_commits: Whether to add some more commits before attempting to restore the outdated bundle.
    """
    bundle = Path("bundle.bundle")
    outdated = Path("outdated.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)
    branch = create_branch(origin, "branched")
    add_commits(origin, branch, count=2)

    backup_bundle_main("create", origin, bundle)

    # Keep the first bundle as the (future) outdated one
    copy(bundle, outdated)

    backup_bundle_main("restore", target, bundle)

    if extra_commits:
        # Make and restore an incremental backup to make the outdated bundle even more outdated
        add_commits(origin, branch)
        backup_bundle_main("create", origin, bundle)
        backup_bundle_main("restore", target, bundle)

    assert_repos_equal(origin, target)

    # Attempting to restore the outdated bundle does not fail (but only with --delete-files)
    backup_bundle_main("restore", target, outdated, "--delete-files")


def test_restore_bundle_with_delete() -> None:
    """
    Verify that restoring a bundle with --delete-files removes the bundle.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle, "--delete-files")

    # Restoring succeeded, the bundle is gone
    assert_repos_equal(origin, target)
    assert not bundle.exists()


def test_restore_bundle_directory_with_delete() -> None:
    """
    Verify that restory bundles from a directory with --delete-files removes the bundle files.
    """
    bundle_dir = Path("bundles")
    bundle = Path("bundle.bundle")
    target = Path("target")

    origin = create_repo("origin")
    add_commits(origin)

    bundle_dir.mkdir()

    backup_bundle_main("create", origin, bundle)

    copy(bundle, bundle_dir / "bundle.bundle")

    backup_bundle_main("restore", target, bundle_dir, "--delete-files")

    # Restoring succeeded, bundle directory is empty
    assert_repos_equal(origin, target)
    assert bundle_dir.exists()
    assert list(bundle_dir.iterdir()) == []

    # Try again with two bundles
    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, bundle_dir / "bundle0.bundle")

    add_commits(origin)
    backup_bundle_main("create", origin, bundle)
    copy(bundle, bundle_dir / "bundle1.bundle")

    backup_bundle_main("restore", target, bundle_dir, "--force", "--delete-files")

    # Restoring succeeded, bundle directory is empty
    assert_repos_equal(origin, target)
    assert bundle_dir.exists()
    assert list(bundle_dir.iterdir()) == []


def test_restore_unrestorable_bundle_with_delete() -> None:
    """
    Verify that restoring a bundle that can't be restored does not delete that bundle even with --delete-files.
    """
    bundle = Path("bundle.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    target = create_repo("target")

    backup_bundle_main("create", origin, bundle)

    add_commits(origin)

    # Create an incremental update. This overwrites bundle.
    backup_bundle_main("create", origin, bundle)

    # Bundle is only an incremental update. It can't be restored.
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundle, "--delete-files")

    # Bundle file has not been deleted
    assert bundle.exists()


def test_restore_fast_forward_reference() -> None:
    """
    Verify that a reference can be fast-forwarded in an incremental update.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)

    target = create_repo("target", bare=True)

    # Create the original branches
    branch = create_branch(origin, "branched", commit=f"{main_branch()}~2")
    add_commits(origin, branch, count=2)
    branch2 = create_branch(origin, "branch2", commit=f"{branch}~1")

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Add a few commits and update the branches
    add_commits(origin, branch)
    change_branch(origin, branch2, new_commit=f"{branch}~1")

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    backup_bundle_main("restore", target, bundles[0])

    # Restoring succeeds without needing --force
    backup_bundle_main("restore", target, bundles[1])

    # Restoring the backups succeeded
    assert_repos_equal(origin, target)


@pytest.mark.parametrize(
    "commit",
    [
        # Lambdas, because main_branch() only becomes available later on
        lambda: f"{main_branch()}~2",  # Update branch to a previous commit (back in history)
        lambda: "sideways_target~1",  # Update branch to a commit on an entirely different branch (rewrite history)
    ],
)
def test_restore_non_fast_forward_reference_update_requires_force(commit: Callable[[], str]) -> None:
    """
    Verify that restoring non-fast-forward reference updates is not allowed without forcing.

    Also verify that restoring *with* --force does work.

    :param commit: The commit to change the branch to (in non-fast-forward fashion).
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)
    branch = create_branch(origin, "to_be_updated", commit=f"{main_branch()}~1")
    extra_branch = create_branch(origin, "sideways_target", commit=f"{main_branch()}~2")
    add_commits(origin, extra_branch, count=2)

    target = create_repo("target", bare=True)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Change the branch in a non-fast-forward fashion, and add a commit so we can make an incremental backup
    change_branch(origin, branch, new_commit=commit())
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    backup_bundle_main("restore", target, bundles[0])

    # Restoring the non-fast-forward reference will fail when restoring without --force ...
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundles[1])

    # ... but should succeed with --force
    backup_bundle_main("restore", target, bundles[1], "--force")

    # Restoring the backups succeeded
    assert_repos_equal(origin, target)


@pytest.mark.parametrize(
    "commit",
    [
        # Lambdas, because main_branch() only becomes available later on
        main_branch,
        lambda: f"{main_branch()}~1",
        lambda: f"{main_branch()}~2",
        lambda: f"{main_branch()}~3",
    ],
)
def test_restore_new_branch_in_incremental_update(commit: Callable[[], str]) -> None:
    """
    Verify that a new branch shows up in an incremental update.

    :param commit: The commit to create the branch on.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=2)

    target = create_repo("target", bare=True)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Add a few commits and the new branch
    add_commits(origin, count=2)
    create_branch(origin, "branched", commit=commit())

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    backup_bundle_main("restore", target, bundles[0])
    backup_bundle_main("restore", target, bundles[1])

    # Restoring the backups succeeded
    assert_repos_equal(origin, target)


@pytest.mark.parametrize(
    "prune",
    [
        True,
        False,
    ],
)
def test_restore_remove_branch_in_incremental_update_requires_prune(*, prune: bool) -> None:
    """
    Verify that a branch may be removed in an incremental update, but only with --prune.

    :param prune: Whether pruning will be used during restore.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)

    target = create_repo("target")

    # Create the branch, with some extra commits
    branch = create_branch(origin, "branched", commit=f"{main_branch()}~2")
    add_commits(origin, branch)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Remove the branch, and add a commit so we can make an incremental backup
    delete_branch(origin, branch)
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # The branch should only be in the first bundle
    assert f"refs/heads/{branch}" in list_reference_names_in_repo(bundles[0])
    assert f"refs/heads/{branch}" not in list_reference_names_in_repo(bundles[1])

    backup_bundle_main("restore", target, bundles[0])

    # The branch is in the restored repo after restoring the first bundle
    assert f"refs/heads/{branch}" in list_reference_names_in_repo(target)

    if prune:
        backup_bundle_main("restore", target, bundles[1], "--force", "--prune")

        # The branch is gone from the restored repo after restoring the second bundle with --prune
        assert f"refs/heads/{branch}" not in list_reference_names_in_repo(target)

        # Restoring the backups (only) fully succeeded with --prune
        assert_repos_equal(origin, target)
    else:
        backup_bundle_main("restore", target, bundles[1], "--force")

        # The branch is still in the restored repo after restoring the second bundle:
        # no pruning without --prune, even though we used --force
        assert f"refs/heads/{branch}" in list_reference_names_in_repo(target)


@pytest.mark.parametrize(
    ("force", "directory", "expect_success"),
    [
        (True, True, False),
        (False, True, False),
        (True, False, True),
        (False, False, False),
    ],
)
def test_restore_incremental_without_new_commits(*, force: bool, directory: bool, expect_success: bool) -> None:
    """
    Verify that restoring an incremental update without new commits only updates references if it's a single file that
    is restored with --force.

    :param force: Whether to use --force when restoring.
    :param directory: Whether to pass the bundle in a directory instead of direct.
    :param expect_success: Whether the combination of force and directory is expected to correctly restore the
                           incremental backup with only reference updates.
    """
    bundle_dir = Path("bundles")
    bundle = (bundle_dir if directory else Path()) / "bundle.bundle"
    target = Path("target")
    snapshot = Path("snapshot")
    restore_from = bundle_dir if directory else bundle

    origin = create_repo("origin")
    add_commits(origin, count=2)
    branch = create_branch(origin, "branch", commit=f"{main_branch()}~1")

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle)

    # Create a second snapshot for comparison
    if not expect_success:
        backup_bundle_main("restore", snapshot, restore_from)

    # Update only the branch and create an incremental backup
    change_branch(origin, branch, new_commit=main_branch())
    backup_bundle_main("create", origin, bundle)

    # Attempt to restore the incremental update
    with nullcontext() if expect_success else pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, restore_from, *(["--force"] if force else []))

    # Check for success by verifying the contents of the repositories
    assert_repos_equal(target, origin if expect_success else snapshot)


def test_restore_update_checked_out_branch_in_clean_worktree_without_force() -> None:
    """
    Verify that updating HEAD in fast-forward fashion in a clean worktree simply updates HEAD and leaves an equally
    clean state.
    """
    bundle = Path("bundle.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    target = create_repo("target", bare=False)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle)

    add_commits(origin)

    backup_bundle_main("create", origin, bundle)

    # Restore the incremental backup after creating a checkout of the main branch (which will be updated)
    call_git(["checkout", main_branch()], cwd=target)
    backup_bundle_main("restore", target, bundle)

    # Restore was successful and left no (staged) changes behind
    assert_repos_equal(origin, target)
    assert not call_git(["status", "--porcelain=1"], cwd=target)
    branch_name, _ = get_current_branch(target)
    assert branch_name == main_branch()


def test_restore_refuses_to_overwrite_uncommitted_changes_in_worktree_without_force() -> None:
    """
    Verify that if there are uncommitted changes in the worktree that would be overwritten by the restore, that will be
    refused unless --force is used.
    """
    bundle = Path("bundle.bundle")
    filename = "some_file"

    origin = create_repo("origin")
    add_commits(origin, count=2, filename=filename)

    target = create_repo("target", bare=False)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle)

    # Dirty the worktree of the target repo, and check that we did
    call_git(["checkout", main_branch()], cwd=target)
    assert not call_git(["diff"], cwd=target)

    target_file = target / filename
    original_target_file_contents = target_file.read_text()
    target_file.write_text(f"{original_target_file_contents} and some more")
    original_change_diff = call_git(["diff"], cwd=target)

    assert original_change_diff
    assert not call_git(["diff", "--cached"], cwd=target)

    # Get us an incremental backup
    add_commits(origin, filename=filename)
    backup_bundle_main("create", origin, bundle)

    # Restoring the incremental backup is refused: "go clean up your workspace"
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundle)

    backup_bundle_main("restore", target, bundle, "--force")

    # The force has updated the underlying HEAD and left the working tree as it was, ...
    assert original_change_diff == call_git(["diff"], cwd=target)

    # ... and has staged an update to revert the new HEAD back to the old one
    assert call_git(["diff", "--cached"], cwd=target)
    call_git(["restore", filename], cwd=target)
    assert target_file.read_text() == original_target_file_contents


def test_restore_remove_checked_out_branch_in_incremental_update_requires_force() -> None:
    """
    Verify the the currently checked out branch can be removed with --prune, but also requires --force.
    """
    bundles = [Path("bundle1.bundle"), Path("bundle2.bundle")]
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)

    target = create_repo("target")

    # Create the branch, with some extra commits
    branch = create_branch(origin, "branched", commit=f"{main_branch()}~2")
    add_commits(origin, branch)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle-location", previous_bundle)

    # Remove the branch, and add a commit so we can make an incremental backup
    delete_branch(origin, branch)
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle-location", previous_bundle)

    # The branch should only be in the first bundle
    assert f"refs/heads/{branch}" in list_reference_names_in_repo(bundles[0])
    assert f"refs/heads/{branch}" not in list_reference_names_in_repo(bundles[1])

    backup_bundle_main("restore", target, bundles[0])

    # The branch is in the target repo after restoring the first bundle
    assert f"refs/heads/{branch}" in list_reference_names_in_repo(target)

    # Create a checkout, and remember it for later comparison
    call_git(["checkout", branch], cwd=target)
    _, checked_out_commit = get_current_branch(target)
    assert checked_out_commit

    # Restoring the bundle fails without --force, even though the workspace is clean and --prune was given
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundles[1], "--prune")

    # Restoring the bundle works, even though the currently checked out branch is removed
    backup_bundle_main("restore", target, bundles[1], "--force", "--prune")

    # The branch is gone from the target repo after restoring the second bundle with --prune
    assert f"refs/heads/{branch}" not in list_reference_names_in_repo(target)

    # Restoring the backups (only) fully succeeded with --prune, did not leave any (staged) changes behind, and left
    # HEAD detached at its previous commit
    assert_repos_equal(origin, target)
    assert not call_git(["status", "--porcelain=1"], cwd=target)
    assert get_current_branch(target) == ("", None)
    assert [checked_out_commit.hash] == call_git(["rev-parse", "HEAD"], cwd=target)


@pytest.mark.parametrize("force", [True, False])
def test_restore_does_not_touch_worktree_even_with_force(*, force: bool) -> None:
    """
    Verify that restoring from a bundle that would not update the currently checked out branch, does not touch the
    worktree even if --force is used.

    :param bool: Whether --force is to be used.
    """
    bundle = Path("bundle.bundle")
    filename = "filename"

    origin = create_repo("origin")
    add_commits(origin)
    branch = create_branch(origin, "branch")
    add_commits(origin, branch, filename=filename)

    target = create_repo("target", bare=False)

    backup_bundle_main("create", origin, bundle)

    backup_bundle_main("restore", target, bundle)

    # Initial commit should leave clean worktree
    assert not call_git(["status", "--porcelain=1"], cwd=target)

    # Dirty the worktree
    call_git(["switch", branch], cwd=target)

    target_filename = target / filename
    target_filename.write_text(target_filename.read_text() + " and a bit more")

    original_target_file_contents = target_filename.read_text()
    original_status = call_git(["status", "--porcelain=1"], cwd=target)
    original_diff = call_git(["diff"], cwd=target)

    # Create a new bundle that doesn't touch that branch
    add_commits(origin)
    backup_bundle_main("create", origin, bundle)

    # Restore incremental backup with --prune and possible even --force, the most destructive operating mode
    backup_bundle_main("restore", target, bundle, *(["--force"] if force else []), "--prune")

    # Worktree has not been touched
    assert original_target_file_contents == target_filename.read_text()
    assert original_status == call_git(["status", "--porcelain=1"], cwd=target)
    assert original_diff == call_git(["diff"], cwd=target)


def test_restore_with_lock_file() -> None:
    """
    Verify that restoring with a lock file works as expected.
    """
    bundle = Path("bundle.bundle")
    target = Path("target")
    lock_file = Path("the_locky")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundle)

    # Restore with a lock file that doesn't exist works fine
    backup_bundle_main("restore", target, bundle, "--lock-file", lock_file)

    # Repository has been restored correctly, lock file is gone again
    assert_repos_equal(origin, target)
    assert not lock_file.exists()

    add_commits(origin)

    backup_bundle_main("create", origin, bundle)

    lock_file.write_text("", encoding=UTF8)

    # Restore with a lock file that already exists does not fail but also doesn't restore anything
    backup_bundle_main("restore", target, bundle, "--lock-file", lock_file, "--delete-files")

    # Incremental backup has not been restored
    assert_repos_not_equal(origin, target)
    assert lock_file.exists()
    assert bundle.exists()

    lock_file.unlink()

    backup_bundle_main("restore", target, bundle, "--lock-file", lock_file, "--delete-files")

    # Incremental backup now has not been restored
    assert_repos_equal(origin, target)
    assert not lock_file.exists()
    assert not bundle.exists()


def test_restore_strict_order_applies_force() -> None:
    """
    Verify that bundles are restored with --force when restoring a directory with --strict-order.

    Also verifies that restoring a directory with --strict-order works.
    """
    bundle_dir = Path("bundles")
    bundles = [
        bundle_dir / "bundle1.bundle",
        bundle_dir / "bundle2.bundle",
    ]
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin)
    create_branch(origin, "b")
    create_branch(origin, "b2")

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    add_commits(origin)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    # Restoring a directory with --strict-order works
    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    assert_repos_equal(origin, target)

    change_branch(origin, "b", new_commit=main_branch())

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    change_branch(origin, "b2", new_commit=main_branch())

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle", previous_bundle)

    # Restoring a directory with --strict-order and --force applies --force to each file in the directory
    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--force", "--delete-files")

    assert_repos_equal(origin, target)


def test_restore_strict_order_does_not_continue_after_failed_restore() -> None:
    """
    Verify that when a single bundle in a directory being restored with --strict-order fails to restore, the remaining
    bundles are not attempted.
    """
    bundle_dir = Path("bundles")
    bundles = [
        bundle_dir / "bundle1.bundle",
        bundle_dir / "bundle2.bundle",
    ]
    target = Path("target")
    intermediate = Path("intermediate")
    previous_bundle = Path("previous.bundle")
    previous_bundle_copy = Path("previous.copy.bundle")

    origin = create_repo("origin")
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    # Backup current state
    backup_bundle_main("restore", intermediate, bundle_dir, "--strict-order")
    copy(previous_bundle, previous_bundle_copy)

    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    add_commits(origin)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    add_commits(origin)

    # Overwrite bundles[0] to create a missing bundle
    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    # Create bundles[1] which contains both new commits and *could* be restored
    backup_bundle_main("create", origin, bundles[1], "--previous-bundle", previous_bundle_copy)

    # Restoring the directory does not restore anything as the first bundle fails to restore
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    assert all(bundle.exists() for bundle in bundles)
    assert_repos_equal(intermediate, target)

    # Restoring the directory is possible without --strict-order thanks to the second bundle
    backup_bundle_main("restore", target, bundle_dir, "--delete-files")

    assert_repos_equal(origin, target)


def test_restore_strict_order_does_not_continue_after_bad_head_update() -> None:
    """
    Verify that when a bundle in a directory being restored with --strict-order would be a bad head update, the
    remaining bundles are not attempted.

    Also verify that a bad head update can still be forced with --strict-order and --force.

    Also verify that --strict-order does not imply --force.
    """
    bundle_dir = Path("bundles")
    bundles = [
        bundle_dir / "bundle1.bundle",
        bundle_dir / "bundle2.bundle",
    ]
    target = Path("target")
    intermediate = Path("intermediate")
    previous_bundle = Path("previous.bundle")
    updated_file = "a_file_in_the_repo"
    dirty_target_file = target / updated_file

    origin = create_repo("origin")
    add_commits(origin, count=3, filename=updated_file)
    create_branch(origin, "b")

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    # Backup current state
    backup_bundle_main("restore", intermediate, bundle_dir, "--strict-order")

    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    call_git(["switch", "b"], cwd=target)
    dirty_target_file.write_text(dirty_target_file.read_text(encoding=UTF8) + "DIRTIED", encoding=UTF8)

    change_branch(origin, "b", new_commit="b~1")
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    # Checkout b and dirty the worktree, so we can't cleanly perform a HEAD update
    change_branch(origin, "b", new_commit=main_branch())
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle", previous_bundle)

    # Restoring the directory does not restore anything as the first bundle fails to restore due to a bad head update
    with pytest.raises(NoBundlesRestoredError):
        backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    assert all(bundle.exists() for bundle in bundles)
    assert_repos_equal(intermediate, target)

    # Restoring the directory is possible with --strict-order and --force.
    # This also verifies that --strict-order did not previously imply --force.
    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--force", "--delete-files")

    assert_repos_equal(origin, target)


def test_restore_strict_order_without_force_does_not_stop_on_reference_only_bundle() -> None:
    """
    Verify that restoring a directory with --strict-order but without --force is not interrupted by a bundle that only
    contains reference updates (and isn't actually restored because it already was).
    """
    bundle_dir = Path("bundles")
    bundles = [
        bundle_dir / "bundle1.bundle",
        bundle_dir / "bundle2.bundle",
    ]
    target = Path("target")
    previous_bundle = Path("previous.bundle")

    origin = create_repo("origin")
    add_commits(origin, count=3)
    create_branch(origin, "b")

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    change_branch(origin, "b", new_commit="b~1")

    backup_bundle_main("create", origin, bundles[0], "--previous-bundle", previous_bundle)

    change_branch(origin, "b", new_commit=main_branch())
    add_commits(origin)

    backup_bundle_main("create", origin, bundles[1], "--previous-bundle", previous_bundle)

    # Restoring the directory works as expected: the reference update bundles[0] is effectively skipped
    backup_bundle_main("restore", target, bundle_dir, "--strict-order", "--delete-files")

    assert_repos_equal(origin, target)
