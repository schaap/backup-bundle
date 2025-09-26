#!/usr/bin/python3

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

"""
Incremental backups of git repositories based on git bundle.

With this script you can create and restore incremental backups for git repositories. The primary use cases are for
environments with strictly separate networks where communication from one network to the other is possible, but
expensive or even impossible in the opposite direction.

Call this script with --help for usage information. Read the accompanying README for more in-depth information, usage
scenarios and development details. If you only have this script file available, then find out who deployed it here and
ask them for the full source.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.config
import re
import subprocess  # noqa: S404  # We heavily rely on subprocess. Given the context for this script it is fine.
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from shutil import copy
from typing import TYPE_CHECKING, ClassVar, cast

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

log: logging.Logger


class ExitCode(IntEnum):
    """Defined non-zero exit codes for this script."""

    ARGPARSE_ERROR = 2
    """Argparse decided that the arguments passed on the command line are not valid."""

    NOTHING_RESTORED = 3
    """The exit code to signal that nothing could be restored."""

    MISSING_REMOTE = 4
    """A remote repository to clone into the repository to back up from was required."""

    GIT_COMMUNICATION_ERROR = 5
    """An unexpected error occurred in communication with git."""

    GIT_CALL_FAILED = 6
    """A call to git failed."""

    EXCEPTION_OCCURRED = 100
    """An unexpected exception occurred."""


class ExitWithExitCodeError(Exception):
    """Request to exit the script with a non-zero exit code."""

    def __init__(self, exit_code: ExitCode, message: str | None = None) -> None:
        """
        Initialize a new ExitWithExitCodeException.

        :param exit_code: The requested exit code.
        :param message: The message to print before exiting.
        """
        super().__init__(*[[message] if message else []])
        self.exit_code = exit_code
        self.message = message


class UnsupportedMetadataError(Exception):
    """A metadata file was read that is not a valid metadata file."""

    def __init__(self, detail: str) -> None:
        """
        Initialize a new UnsupportedMetadataException.

        :param message: The detail message.
        """
        super().__init__(f"The metadata file is invalid. {detail}")


class InternalInconsistencyError(Exception):
    """An error in internal consistency has occurred."""

    def __init__(self, detail: str) -> None:
        """
        Initialize a new InternaInconsistencyError.

        :param message: The detail message.
        """
        super().__init__(
            "An internal inconsistency has been detected. Please report this at "
            f"https://github.com/schaap/backup-bundle/. Aborting.\n{detail}"
        )


class MissingRemoteError(ExitWithExitCodeError):
    """The repository to back up from did not yet exist, but no --remote was given."""

    def __init__(self, repo: Path) -> None:
        """
        Initialize a new MissingRemoteException.

        :param repo: The repository that would need to be initialized with a clone.
        """
        super().__init__(
            ExitCode.MISSING_REMOTE, f"Repository {repo} does not yet exist, but no --remote to clone from was given."
        )


class GitCallFailedError(ExitWithExitCodeError):
    """git returned a non-zero exit code."""

    def __init__(self, stderr: str) -> None:
        """
        Initialize a new GitCallFailedError.

        :param stderr: The stderr from git.
        """
        super().__init__(
            ExitCode.GIT_CALL_FAILED,
            f"Call to git failed. git reported:\n{stderr}\nRetry with -v for more information.",
        )


class GitCommunicationError(ExitWithExitCodeError):
    """
    An error occurred in the communication with git.

    Note that this is not just a failed git call: use `GitCallFailedError` for that.
    """

    def __init__(self, detail: str) -> None:
        """
        Initialize a new GitCommunicationException.

        :param message: The detail message.
        """
        super().__init__(ExitCode.GIT_COMMUNICATION_ERROR, f"Unexpected error in communication with git. {detail}")


class NoBundlesRestoredError(ExitWithExitCodeError):
    """No bundles were restored."""

    def __init__(self) -> None:
        """
        Initialize a new NoBundlesRestoredException.
        """
        super().__init__(ExitCode.NOTHING_RESTORED)


class SimpleLockFileNotCreatedError(Exception):
    """The simple lock file could not be created, i.e. the lock was not granted."""


class GitRef:
    """A git reference."""

    ref_pattern: ClassVar[re.Pattern[str]] = re.compile(r"(?P<hash>[a-fA-F0-9]*)\s+(?P<ref>.*)")
    """The pattern to parse the output of git show-ref."""

    def __init__(self, commit_hash: str, ref: str) -> None:
        """
        Initialize a new GitRef.

        :param commit_hash: The hash of the referenced commit.
        :param ref: The full git reference name (e.g. "refs/heads/main").
        """
        self._hash = commit_hash
        self._ref = ref

    @classmethod
    def from_show_ref(cls, show_ref_line: str) -> GitRef:
        """
        Create a new GitRef from a line of output from git show-ref.

        :param show_ref_line: A single output line from git show-ref.
        :return: The corresponding new GitRef instance.
        """
        match = cls.ref_pattern.match(show_ref_line)
        if not match:
            raise GitCommunicationError(f"Parsing show-ref(-like) output failed on: {show_ref_line}")
        return cls(match.group("hash"), match.group("ref"))

    @property
    def hash(self) -> str:
        """The hash of the references commit."""
        return self._hash

    @property
    def ref(self) -> str:
        """The full git reference name (e.g. "refs/heads/main")."""
        return self._ref

    def __str__(self) -> str:
        return f"{self._ref}: {self._hash}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._hash!r}, {self._ref!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GitRef) and (self._hash, self._ref) == (other._hash, other._ref)

    def __hash__(self) -> int:
        return hash(self._hash) + hash(self._ref)


@dataclass
class Metadata:
    """The contents of a metadata file."""

    CURRENT_VERSION: ClassVar[int] = 1
    """The current version of the metadata file."""

    version: int
    """The version of this metadata structure."""

    known_tag_refs: list[str] = field(default_factory=list[str])
    """The list of tag references (i.e. `refs/tags/*`) which have been backed up."""

    def __post_init__(self) -> None:
        # Verify the version. We really shouldn't interpret data of an unsupported version.
        if not isinstance(self.version, int):
            raise UnsupportedMetadataError("The version field must be of type int")
        if self.version != Metadata.CURRENT_VERSION:
            raise UnsupportedMetadataError(f"Only version {Metadata.CURRENT_VERSION} is supported")

        # Verify the datatypes of the other fields. This makes it easier to construct blindly from imported JSON
        # (or similar formats).
        if not isinstance(self.known_tag_refs, list):
            raise UnsupportedMetadataError("The known_tag_refs field must be a list of strings")
        if not all(isinstance(known_tag_ref, str) for known_tag_ref in self.known_tag_refs):
            raise UnsupportedMetadataError("The known_tag_refs field must be a list of strings")


@contextmanager
def simple_lock_file(lock_file: Path) -> Generator[None, None, None]:
    """
    Create a simple lock file (an inter-process "lock" based on exclusive-create).

    If created with success, the file will be removed again upon leaving the context.

    :param lock_file: The file to use as a lock file.
    :raises SimpleLockFileNotCreatedError: When creating the lock failed.
    """
    try:
        with lock_file.open(mode="x", encoding="utf-8") as f:
            try:
                yield
            finally:
                f.close()
                lock_file.unlink()
    except FileExistsError:
        raise SimpleLockFileNotCreatedError from None


def _call_git(arguments: list[str], *, cwd: Path) -> list[str]:
    """
    Run a git process and return it's output. Internal helper without error handling.

    :param arguments: The arguments to call git with (e.g. `["status"]` to call `git status`)
    :param cwd: The working directory from which to call git.
    :raises subprocess.CalledProcessError: If the call to git fails.
    """
    log.debug("Calling: git %s", " ".join(arguments))
    process = subprocess.run(  # noqa: S603  # Arguments have been compiled by us, they're fine
        ["git", *arguments],  # noqa: S607  # We rely on PATH to find git for us, for compatibility
        cwd=str(cwd),
        capture_output=True,
        check=True,
        text=True,
    )
    return process.stdout.splitlines()


def call_git(arguments: list[str], *, cwd: Path) -> list[str]:
    """
    Run a git process and return it's output.

    :param arguments: The arguments to call git with (e.g. `["status"]` to call `git status`)
    :param cwd: The working directory from which to call git.
    :raises GitCallFailedError: If the call to git failed.
    """
    try:
        return _call_git(arguments, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        log.debug("Call failed. stdout=%s, stderr=%s", exc.stdout, exc.stderr)
        if exc.returncode != 0:
            raise GitCallFailedError(exc.stderr) from exc
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


def exclusion_list(to_exclude: Iterable[str]) -> list[str]:
    """
    Prepend a list with "--not" for use as a git rev-list exclusion.

    Use this function to prevent an empty --not clause.

    :param to_exclude: The list of references to exclude.
    :return: `to_exclude` prepended by "--not", or an empty list if `to_exclude` is empty.
    """
    return ["--not", *to_exclude] if to_exclude else []


class Backup:
    """
    Context object for creating a backup bundle from a repository.
    """

    def __init__(self, repo: Path, remote: str | None, *, mirror: bool) -> None:
        """
        Initialize a new Backup.

        :param repo: The source repository.
        :param remote: The remote to clone from if the source repository needs to be created.
        :param mirror: If set, operate the repository in mirror mode. This implies a `git remote update --prune` before
                       creating the backup bundle.
        """
        self.repo = repo
        self.mirror = mirror

        self._ensure_source_is_repo(self.repo, remote)

    def _ensure_source_is_repo(self, repo: Path, remote: str | None) -> None:
        """
        Create the source repository if it doesn't exist or is an empty directory.

        In all other cases repo is assumed to be a git repository and git commands will fail later on if it isn't.

        :param mirror: If set, clone into the source repository in mirror mode, if cloning is needed.
        """
        if not repo.exists() or not any(repo.iterdir()):
            log.info("Cloning %s into new repository %s", remote, repo)
            if remote is None:
                raise MissingRemoteError(repo)
            repo.mkdir(parents=True, exist_ok=True)
            # Even if we're not mirroring (and hence create normal repository), a checkout is a waste of resources
            mirrored = ["--mirror"] if self.mirror else ["--no-checkout"]
            call_git(["clone", "--no-hardlinks", *mirrored, remote, str(repo)], cwd=Path())

    def perform_backup(
        self, bundle: Path, stored_bundle: Path, metadata_file: Path | None, *, timestamped: bool, skip_unchanged: bool
    ) -> bool:
        """
        Create a new backup bundle with the settings in this Backup object.

        :param bundle: The bundle file to write the backup to.
        :param stored_bundle: The bundle file that is used as a reference for the previous backup.
        :param metadata_file: If set, a file with metadata about the (previous) backup. This enabled the backup of tags.
        :param timestamped: If set, the filename `bundle` has a timestamp added.
        :param skip_unchanged: If set, no bundle file is written if it does not contain new commits or updated
                               references compared to `stored_bundle`.
        :returns: Whether a backup bundle was written.
        """

        # If we're working in mirror mode, trigger a remote update, first
        if self.mirror:
            call_git(["remote", "update", "--prune"], cwd=self.repo)

        # Adjust bundle if a timestamped version is requested
        if timestamped:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            bundle = bundle.parent / f"{bundle.stem}.{timestamp}Z{bundle.suffix}"

        # Create the incremental backup
        metadata, bundle_written = self._create_incremental_bundle(
            bundle.absolute(),
            stored_bundle.absolute(),
            metadata_file.absolute() if metadata_file else None,
            skip_unchanged=skip_unchanged,
        )

        # No need to continue if no bundle file was written
        if not bundle_written:
            return False

        # Save the bundle and its metadata as reference points for the next incremental backup.
        if bundle.resolve() != stored_bundle.resolve():
            copy(bundle, stored_bundle)
        if metadata_file:
            metadata_file.write_text(json.dumps(asdict(metadata)))
            log.info("Written backup metadata to %s", metadata_file)

        return True

    def _create_incremental_bundle(
        self, bundle: Path, stored_bundle: Path, metadata_file: Path | None, *, skip_unchanged: bool
    ) -> tuple[Metadata, bool]:
        """
        Create an incremental backup bundle from a repository.

        This will create a new bundle containing all references in the repository as well as all new commits compared to
        the `backup` remote in the repository.

        :param bundle: The file to write the bundle to. Parent directories will be created. If this is relative, it's
                       interpreted relative to the repository.
        :param stored_bundle: The bundle file that is used as a reference for the previous backup.
        :param metadata_file: If set, a file with metadata about the (previous) backup. This enabled the backup of tags.
        :param skip_unchanged: If set, no bundle file is written if it does not contain new commits or updated
                               references compared to `stored_bundle`.
        :return: A Metadata that accompanies the written bundle file, and whether the bundle file has been written.
        """
        # Read the metadata file
        metadata: Metadata | None
        if metadata_file:
            if metadata_file.exists():
                log.info("Using previous backup metadata from %s.", metadata_file)
                metadata = Metadata(**json.loads(metadata_file.read_text()))
            else:
                log.info("No previous backup metadata found at %s. Using empty metadata.", metadata_file)
                metadata = Metadata(version=Metadata.CURRENT_VERSION)
        else:
            metadata = None

        # Find all the local git references: they all need to be included in the bundle. This ensures not only that we
        # add all reachable commits since the previous backup, but also that we include *all* references (allowing a
        # `git fetch --all --prune` from the incremental bundle to correctly update the copy). Tags are only included if
        # we can track them, but they're exempt from pruning, anyway.
        include_tags = ["--tags"] if metadata else []
        refs_to_include = [
            GitRef.from_show_ref(ref_line)
            for ref_line in call_git(["show-ref", "--heads", *include_tags], cwd=self.repo)
        ]

        # If tags are to be backed up, we will only include new tags. See `git tag`'s manual page, section
        # "On Retagging": retagging really should never occur and tags can be consider write-once (under normal
        # circumstances git also does this). So it's fine if we only back up new tags. (Note that this filter strictly
        # speaking filters all refs, not just tags. Saving anything but tag references in the metadata file is simply an
        # error.)
        if metadata:
            refs_to_include = [ref for ref in refs_to_include if ref.ref not in metadata.known_tag_refs]

        # Find all the commits pointed to by the references in the previous backup (by querying the previous bundle
        # directly): they should all be excluded from the new bundle. This makes it an incremental backup.
        previous_backup_ref_commits: list[str] = []
        previous_backup_references: list[GitRef] = []
        if stored_bundle.exists():
            previous_backup_references = [
                GitRef.from_show_ref(ref_line)
                for ref_line in call_git(["ls-remote", "--heads", *include_tags, str(stored_bundle)], cwd=Path())
            ]
            previous_backup_ref_commits = [ref.hash for ref in previous_backup_references]

        # Calculate the list of all new commits in this repository, compared to the previous bundle. These commits will
        # all be included in the new bundle.
        new_commits = set(
            call_git(
                ["rev-list", *[ref.hash for ref in refs_to_include], *exclusion_list(previous_backup_ref_commits)],
                cwd=self.repo,
            )
        )

        # Not having new commits would make the bundle a bit of an odd case. In particular it will lack ordering among
        # other incremental bundles, which may cause unwanted effects if it's still used for restoring references.
        if not new_commits:
            if skip_unchanged:
                # There are no new commits. If none of the references have been updated, either, then we're done since
                # it has been requested to only write a bundle file if something has changed.
                if not set(previous_backup_references) ^ set(refs_to_include):
                    log.info("No changes detected. Not creating a new bundle, as requested.")
                    return metadata or Metadata(version=Metadata.CURRENT_VERSION), False
            else:
                # Without new commits, normal operation will still write a new bundle, but the user will need to know
                # how to correctly import it (if they want to).
                log.warning(
                    "Bundle %s will not contain any new commits. Restoring this bundle will be a no-op and will "
                    "not update any references. To force restoring the bundle anyway, pass the filename of the "
                    "exact bundle (as opposed to the directory containing it) and --force when restoring.",
                    bundle,
                )

        # Next to the new commits, the commit that each reference in the repository points to must be included, as well.
        # This is required to allow those references to be included in the new bundle.
        all_commits_to_include = new_commits | {ref.hash for ref in refs_to_include}

        # Consider the list of commits that would be excluded by each single exclusion. Keep those exclusions that do
        # not exclude commits we explicitly wish to include. This will provide the most stringent restriction on commit
        # selection for the new bundle without excluding something that should have been included.
        included_refs_parent_commit = [
            maybe_commit[0]
            for maybe_commit in [try_call_git(["rev-parse", f"{ref.hash}~1"], cwd=self.repo) for ref in refs_to_include]
            if maybe_commit
        ]
        reachable_commits_per_exclusion = {
            exclusion_ref: set(call_git(["rev-list", exclusion_ref], cwd=self.repo))
            for exclusion_ref in [*previous_backup_ref_commits, *included_refs_parent_commit]
        }
        use_exclusions = [
            exclusion_ref
            for exclusion_ref, excluded_commits in reachable_commits_per_exclusion.items()
            if excluded_commits.isdisjoint(all_commits_to_include)
        ]

        # Create the incremental bundle
        bundle.parent.mkdir(parents=True, exist_ok=True)
        call_git(
            [
                "bundle",
                "create",
                str(bundle),
                # References to include must be named, not given by hash: bundles are created for references, not for
                # loose commits. This is an exception to the usual `rev-list-arguments` that `git bundle create`
                # otherwise accepts.
                *[ref.ref for ref in refs_to_include],
                # All exclusions may be passed in any form acceptable for `git rev-list`. So we can just use the commit
                # hashes.
                *exclusion_list(use_exclusions),
            ],
            cwd=self.repo,
        )

        # Return the metadata, even if we weren't provided with one to begin with.
        if not metadata:
            metadata = Metadata(version=Metadata.CURRENT_VERSION)

        new_tag_refs = {ref.ref for ref in refs_to_include if ref.ref.startswith("refs/tags/")}
        metadata.known_tag_refs = list(set(metadata.known_tag_refs) | new_tag_refs)
        return metadata, True


def are_available(repo: Path, commits: list[GitRef]) -> bool:
    """
    Verify whether all commits are already available in a repository.

    :param repo: The repository to query.
    :param commits: The list of commits to check.
    :return: True iff all commits are available in the repository.
    """
    # Just try and list the commit using rev-list. Using `try_call_git` this will list the commit if it exists (Trueish)
    # or give an empty list if the call fails due to the commit not existing (False-ish).
    return all(try_call_git(["rev-list", "-n", "1", commit.hash], cwd=repo) for commit in commits)


def get_current_branch(repo: Path) -> tuple[str, GitRef | None]:
    """
    Obtain the name and `GitRef` for the currently checked out branch.

    A new repository will return `("main", None)` (or whichever the main branch name is). A detached head will return
    `("", None)`.

    :param repo: The repository to query.
    :return: The name of the currently checked out branch, and the corresponding `GitRef`.
    """
    # Find out which branch is currently checked out in the repo (change checked-out branch)
    current_branch: GitRef | None = None

    # current_branch_name can be empty in detached head mode
    show_current = call_git(["branch", "--show-current"], cwd=repo)
    current_branch_name = show_current[0] if show_current else ""

    if current_branch_name:
        # show_ref would be empty (show-ref fails) if the branch has no commits yet (new repo)
        show_ref = try_call_git(["show-ref", f"refs/heads/{current_branch_name}"], cwd=repo)

        if show_ref:
            current_branch = GitRef.from_show_ref(show_ref[0])

    return (current_branch_name, current_branch)


def list_references_in_repo(repo: Path) -> list[GitRef]:
    """
    List all the references in a (remote) repository.

    :param repo: The repository to list all references for. This may be a bundle file.
    :return: All the references (heads and tags) in repo.
    """
    return [
        GitRef.from_show_ref(ref_line)
        for ref_line in call_git(["ls-remote", "--heads", "--tags", str(repo.absolute())], cwd=Path())
    ]


def is_bare_repo(repo: Path) -> bool:
    """
    Check if a repository is bare.

    :param repo: The repository to check.
    """
    result = call_git(["rev-parse", "--is-bare-repository"], cwd=repo)[0].strip()
    if result == "true":
        return True
    if result == "false":
        return False
    raise GitCommunicationError(f"Unexpected result for git rev-parse --is-bare-repository: {result}")


class Restoration:
    """
    Context object for restoring one or more bundles to a repository.
    """

    def __init__(self, repo: Path, *, bare: bool, force: bool, prune: bool, delete_files: bool) -> None:
        """
        Initialize a new restoration object.

        This will already make sure that repo is initialized, if needed.

        :param repo: The repository to restore data to. If this is empty or does not exist yet, a new repository will
                     be cloned from the bundle.
        :param bare: If set and a new repository needs to be created, create a bare repository.
        :param force: If set, all references will be updated, even if the update is not a fast-forward or on the branch
                      that is currently checked out. If not set bundles that would perform such an update can't be
                      restored.
        :param prune: If set, remove all branches that are not present in the bundle.
        :param delete_files: If set, bundle files that are successfully restored will be deleted. If not set and a
                             directory is passed to restore from, the check whether it has already been restored may
                             become expensive.
        """
        self.repo = repo
        self.force = force
        self.delete_files = delete_files

        # Tracking of bundles being restored
        self.skip_bundles: set[Path] = set()
        self.restored_bundle_count = 0

        # Make sure we can use the target repository
        self._ensure_target_is_repo(bare=bare)
        self._is_repo_bare = is_bare_repo(self.repo)

        # Find out which branch is currently checked out in the repo
        self.current_branch_name, self.current_branch = get_current_branch(repo)

        # Determine additional flags to `git fetch`
        # Note the absence of --prune-tags: retagging is not supported. A tag is final (as it's intended to be by git).
        self.force_and_prune = [*(["--prune"] if prune else []), *(["--force", "--update-head-ok"] if force else [])]

    def _ensure_target_is_repo(self, *, bare: bool) -> None:
        """
        Create the target repository if it currently does not exist or is an empty directory.

        In all other cases the repository will be assumed to be a git repository. git commands will just fail later on
        if it isn't.

        :param bare: If set, create a bare repository if a repository is to be created.
        """
        if not self.repo.exists() or not any(self.repo.iterdir()):
            log.info("Creating new repository in %s", self.repo)
            self.repo.mkdir(parents=True, exist_ok=True)
            bare_options = ["--bare"] if bare else []
            call_git(["init", *bare_options, "."], cwd=self.repo)

    def _mark_bundle_restored(self, bundle: Path, *, was_already_restored: bool = False) -> bool:
        """
        Mark the bundle as restored.

        :param bundle: The restored bundle.
        :param was_already_restored: If all commits in the bundle were already available before even attempting to
                                     restore it.
        :return: True iff this warrants a new sweep over all bundles that have not yet been restored.
        """
        self.skip_bundles.add(bundle)

        if self.delete_files:
            bundle.unlink()

        if not was_already_restored:
            # If a bundle is successfully restored, we should go over all non-restored bundles again. This ensures that
            # the order of bundles in a bundle directory is insignificant (unless they're conflicting, but wut?).
            self.restored_bundle_count += 1
            return True

        if self.delete_files:
            self.restored_bundle_count += 1

        return False

    def _need_detach_head_first(self, new_references: list[GitRef]) -> bool:
        """
        Check whether HEAD in the local repository should be detached first to allow for the current checked out branch
        to be deleted.

        :param new_references: The new references to update the local repository to.
        :return: True iff HEAD should be detached before (force-)fetching the updates.
        """
        if self.current_branch_name:
            new_ref_for_current_branch = [
                ref for ref in new_references if ref.ref == f"refs/heads/{self.current_branch_name}"
            ]
            if self.force and not new_ref_for_current_branch:
                # This will delete the current branch. HEAD needs to be detached first.
                return True
        return False

    def _is_bad_head_update(self, new_references: list[GitRef]) -> bool:
        """
        Check whether updating the local repository to the references in new_references would update the currently
        checked out branch in an impermissable way.

        Warn and guide the user if an update to the currently checked out branch is detected that won't be performed,
        i.e. when True is returned.

        :param new_references: The new references to update the local repository to.
        :return: True iff the update to HEAD is not allowed.
        """
        if not self.current_branch_name:
            return False

        new_ref_for_current_branch = [
            ref for ref in new_references if ref.ref == f"refs/heads/{self.current_branch_name}"
        ]
        if new_ref_for_current_branch:
            if self.force or self._is_repo_bare:
                # Updating HEAD in a bare repo or with --force is always OK
                return False
            if self.current_branch and new_ref_for_current_branch[0].hash == self.current_branch.hash:
                # The currently checked out branch will not be updated.
                return False
            status = call_git(["status", "--porcelain=1"], cwd=self.repo)
            if not status:
                # The workspace is clean. We can advance the current HEAD without fear of destroying data.
                # (Note that this also covers the case where the repository is new.)
                return False
            log.debug("Worktree is not clean. git status --porcelain=1 returned:\n%s", "\n".join(status))
            log.warning(
                "The currently checked out branch would be updated. Please stash your changes and clean your worktree "
                "(also remove untracked files) or use --force to force your current branch to be updated nonetheless "
                "(THIS WILL DELETE ALL UNCOMMITTED CHANGES!)."
            )
        else:
            if self.force:
                return False
            log.warning(
                "The currently checked out branch would be deleted. Please change to a different branch or use --force "
                "to force your current branch to be removed nonetheless (this will leave you with a detached HEAD)."
            )

        return True

    def _explicitly_update_current_head(self, new_references: list[GitRef]) -> tuple[str | None, bool]:
        """
        Check if an explicit update of the current head is required.

        :param new_references: The new references to update the local repository to.
        :return: The commit to reset the current banch to, if needed, and whether to include --update-head-ok as an
                 argument to git fetch.
        """
        if self.current_branch_name:
            new_ref_for_current_branch = [
                ref for ref in new_references if ref.ref == f"refs/heads/{self.current_branch_name}"
            ]
            if new_ref_for_current_branch and not self.force and not self._is_repo_bare:
                if self.current_branch and new_ref_for_current_branch[0].hash == self.current_branch.hash:
                    # The currently checked out branch will not be updated. git might still complain, so explicity allow
                    # it to do nothing.
                    return (None, True)
                status = call_git(["status", "--porcelain=1"], cwd=self.repo)
                if not status:
                    # The workspace is clean. We can advance the current HEAD without fear of destroying data.
                    # (Note that this also covers the case where the repository is new.)
                    return (new_ref_for_current_branch[0].hash, True)
        return (None, False)

    def _dry_run_git_fetch(self, bundle: Path, git_fetch_arguments: list[str]) -> None:
        """
        Attempt a dry run of git fetch, with detection for reference updates that are not fast-forward.

        The detection assumes that updates to HEAD have already been detected and handled.

        A warning will be logged if the absence of --force keeps the bundle from being restored. The caller will not be
        notified of this in any way.

        :param bundle: The bundle being fetched.
        :param git_fetch_arguments: All arguments to `git fetch`.
        :raises GitCallFailedError: If the dry-run failed.
        """
        try:
            call_git(["fetch", "--dry-run", *git_fetch_arguments], cwd=self.repo)
        except GitCallFailedError:
            if not self.force:
                # Detect non-fast-forward updates by just retrying the dry-run with --force. Note that updates
                # to HEAD (the other reason for the non-forced dry-run to fail) have already been detected.
                try:
                    call_git(["fetch", "--dry-run", "--force", *git_fetch_arguments], cwd=self.repo)
                    log.warning(
                        "Bundle %s can't be restored. Updates to references that are not fast-forward "
                        "are only allowed with --force.",
                        bundle,
                    )
                except GitCallFailedError:
                    pass
            raise

    def _perform_bundle_restore(
        self,
        bundle: Path,
        new_references: list[GitRef],
        *,
        force_update_head: bool,
        detach_head: bool,
        reset_current_branch_to: str | None,
    ) -> bool:
        """
        Perform the actual bundle restore.

        :param bundle: The bundle being fetched.
        :param new_references: The new references that are available in bundle.
        :param force_update_head: Whether an update to head must be forcibly allowed using --update-head-ok.
        :param detach_head: Whether to detach HEAD before fetching all updates.
        :param reset_current_branch_to: If not None, the commit to hard reset the current branch to before fetching all
                                        updates.
        :return: Whether the bundle was fully restored.
        """
        git_fetch_arguments = [
            "--atomic",
            "--tags",
            "--no-write-fetch-head",
            *self.force_and_prune,
            # git would complain about fetching into the current branch, even if that doesn't update anything. Bypass
            # the check with --update-head-ok if we will fix it for git ourselves.
            *(["--update-head-ok"] if force_update_head else []),
            str(bundle.absolute()),
            "refs/heads/*:refs/heads/*",
            # Note that we can't use refs/tags/*:refs/tags/* : that would start purging tags even without
            # --prune-tags. See `git fetch` documentation, section Pruning, for more details.
            *[f"{ref.ref}:{ref.ref}" for ref in new_references if ref.ref.startswith("refs/tags/")],
        ]

        log.info("Attempting to restore %s", bundle)

        # Perform checks and a dry-run first, to ensure that changes are only made if the entire bundle can be
        # processed. This is not only nicer for the user, but keeps the bundle available for another attempt later on
        # (as opposed to being seen as fully restored due to all the commits already having been fetched into the
        # repository).
        try:
            call_git(["bundle", "verify", str(bundle.absolute())], cwd=self.repo)

            # We can't perform the manual steps for current branch handling in a true dry-run. They shouldn't fail if
            # the dry-run otherwise succeeds, and the only difference would be whether the check for updating HEAD
            # fails. So disable that check during the dry-run for those cases.
            simulate_head_update = ["--update-head-ok"] if detach_head else []

            self._dry_run_git_fetch(bundle, [*simulate_head_update, *git_fetch_arguments])
        except GitCallFailedError:
            # Restoring bundle failed. This can occur if we're missing data. It's not an error.
            return False

        # After the checks above, actually restoring the bundle is expected not to fail.
        if reset_current_branch_to:
            if not self.current_branch_name:
                raise InternalInconsistencyError("use_reset_for_current_branch is set, but not current_branch_name")

            call_git(
                [
                    "fetch",
                    "--prefetch",
                    "--no-write-fetch-head",
                    str(bundle.absolute()),
                    f"refs/heads/{self.current_branch_name}:refs/heads/{self.current_branch_name}",
                ],
                cwd=self.repo,
            )
            call_git(["reset", "--hard", reset_current_branch_to], cwd=self.repo)
            call_git(["update-ref", "-d", f"refs/prefetch/heads/{self.current_branch_name}"], cwd=self.repo)

        if detach_head:
            call_git(["switch", "--detach"], cwd=self.repo)

        # Actual attempt to fully restore the bundle.
        call_git(["fetch", *git_fetch_arguments], cwd=self.repo)

        log.info("Restored bundle %s", bundle)

        return True

    @staticmethod
    def _list_bundles(bundle: Path) -> list[Path]:
        """
        List the bundle files found.

        :param bundle: The path to list bundle files for.
        :returns: A list with just `bundle` if that is a file, otherwise all files with the `.bundle` extension in that
                  directory. The list will be sorted.
        """
        if bundle.is_dir():
            bundles = [f for f in bundle.iterdir() if f.is_file() and f.suffix == ".bundle"]
        else:
            bundles = [bundle]

        # Sort the bundles by filename for optimization
        bundles.sort()

        return bundles

    def restore_bundles(self, bundle: Path, *, strict_order: bool) -> int:
        """
        Restore data from one or more bundles.

        The bundles will be restored to this Restoration's repository, with the settings given during initialization.

        :param bundle: The bundle to restore from. If a directory is passed, any files in it with the `.bundle`
                       extension will be attempted (repeatedly, until none of them can restore anymore). Directory
                       handling is optimal if the bundle files can be restored in alphabetical order.
        :param strict_order: If set, bundle files in a directory will be processed strictly in order of their filenames,
                             with no cycling over the bundles to attempt to restore other bundles when one fails to be
                             restored.
        :return: The total number of bundles found. If bundle is a file this is 1, otherwise it's the number of bundle
                 files found in the directory bundle points to.
        """
        # Figure out which bundle files to handle
        bundles = Restoration._list_bundles(bundle)
        log.info("Found %d bundles to restore.", len(bundles))

        # Find the references included in each bundle
        references = {bundle: list_references_in_repo(bundle) for bundle in bundles}

        restore_more_bundles = True
        while restore_more_bundles:
            restore_more_bundles = False

            # Attempt to restore bundles one by one
            for current_bundle in bundles:
                # Do not attempt these again
                if current_bundle in self.skip_bundles:
                    continue

                # Do not attempt to restore a bundle that is already fully available
                # Outdated bundles are "restored" this way, without git complaining about not-fast-forwarding branches
                # Note that a negative result can't be cached: if bundle A is not available nor can't be restored, then
                # bundle B being restored can actually mean that A has been restored when this is attempted again (A was
                # a strict subset of B, apparently).
                #
                # A single file can be force-restored. This allows recovering from (some) errors.
                #
                # Files in a directory can also be force-restored when they're being processed in strict order. This
                # facilitates even closer mirroring a repository by continuously providing updates, even if those only
                # contain reference updates.
                apply_force = self.force and (strict_order or bundle.is_file())
                if not apply_force and are_available(self.repo, references[current_bundle]):
                    log.warning("Bundle %s has already been restored", current_bundle)
                    restore_more_bundles |= self._mark_bundle_restored(current_bundle, was_already_restored=True)
                    continue

                # Detect updates to the currently checked out branch. git is difficult about these. In some cases we
                # shouldn't allow them, in other cases it might be permissable but only by performing some
                # additional strategies.
                if self._is_bad_head_update(references[current_bundle]):
                    log.warning(
                        "Bundle %s can't be restored: it would update the currently checked out branch. "
                        "This is only allowed with --force.",
                        current_bundle,
                    )
                    # This issue remains no matter how many other bundles we restore
                    self.skip_bundles.add(current_bundle)
                    # Continue with the next bundle file, unless they were to be processed in strict order.
                    if strict_order:
                        break
                    continue

                first_detach_head = self._need_detach_head_first(references[current_bundle])
                first_reset_current_branch_to, force_update_head = self._explicitly_update_current_head(
                    references[current_bundle]
                )

                # Attempt to restore this bundle
                if not self._perform_bundle_restore(
                    current_bundle,
                    references[current_bundle],
                    force_update_head=force_update_head,
                    detach_head=first_detach_head,
                    reset_current_branch_to=first_reset_current_branch_to,
                ):
                    # Continue with the next bundle file, unless they were to be processed in strict order.
                    if strict_order:
                        break
                    continue

                restore_more_bundles |= self._mark_bundle_restored(current_bundle)

            # When bundle files are to be processed in strict order, never attempt to restore more bundles in another
            # loop over the directory.
            if strict_order:
                restore_more_bundles = False
        return len(bundles)


class Actions(Enum):
    """Valid actions for this script."""

    Create = "create"
    """Create a new backup bundle."""

    Restore = "restore"
    """Restore data from a backup bundle."""


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Add the common argument groups to an argument parser.

    :param parser: The parser to add the argument groups to.
    """
    log_parser = parser.add_argument_group(
        "Logging configuration",
        "Configure the python logging facilities. By default a very simple stream handler is used to just print "
        "messages to stdout.",
    )
    log_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Provide debugging level output. This will include the underlying calls to git and the output of any such "
            "calls that failed."
        ),
    )
    log_config_parser = log_parser.add_mutually_exclusive_group()
    log_config_parser.add_argument(
        "--log-config-file",
        action="store",
        type=Path,
        help=(
            "Read a logging configuration dictionary from this JSON file. See python's logging.config.dictConfig for "
            "the contents of the dictionary."
        ),
    )
    log_config_parser.add_argument(
        "--log-config",
        action="store",
        type=str,
        help=(
            "Use this JSON string as a logging configuration dictionary. See python's logging.config.dictConfig for "
            "the contents of the dictionary."
        ),
    )


def configure_logging(args: argparse.Namespace) -> None:
    """
    Configure the logging.

    :param args: The arguments as parsed from the command line.
    """
    # The logger is only created here to avoid burdening the user with preventing logging lock out by forcing them to
    # set incremental or disable_existing_loggers, or have them explicitly configure our logger.
    global log  # noqa: PLW0603

    verbose = bool(args.verbose)
    log_config_file = cast(Path | None, args.log_config_file)
    log_config = cast(str | None, args.log_config)

    if log_config_file:
        logging.config.dictConfig(json.loads(log_config_file.read_text()))
        log = logging.getLogger(__name__)
    elif log_config:
        logging.config.dictConfig(json.loads(log_config))
        log = logging.getLogger(__name__)
    else:
        log = logging.getLogger(__name__)
        log.setLevel(logging.INFO)
        if not log.handlers:
            log.addHandler(logging.StreamHandler())
            log.handlers[0].setLevel(logging.DEBUG if verbose else logging.INFO)

    if verbose:
        log.setLevel(logging.DEBUG)


def main(argv: list[str]) -> None:
    """
    Run the bundle script.

    :param argv: The command line arguments, excluding this script itself (which would usually occur in `sys.argv[0]`).
    :raises ExitWithExitCodeException: If exit with a non-zero exit code is requested.
    """
    parser = argparse.ArgumentParser(
        prog="bundle", description="Perform git backups using git bundle.", exit_on_error=False
    )
    subparsers = parser.add_subparsers(title="Action", required=True, dest="action")
    backup_parser = subparsers.add_parser(
        Actions.Create.value, help="Create an (incremental) backup of a repository to a bundle file."
    )
    restore_parser = subparsers.add_parser(
        Actions.Restore.value, help="Restore the contents of one or more bundle files to a repository."
    )

    backup_parser.add_argument("repo", action="store", type=Path, default=Path(), help="The repository to backup from.")
    backup_parser.add_argument(
        "bundle", action="store", type=Path, default=Path("backup.bundle"), help="The bundle file to create"
    )
    backup_parser.add_argument(
        "-r",
        "--remote",
        action="store",
        help="The remote repository to clone if --repo points to an empty or non-existent directory.",
    )
    backup_parser.add_argument(
        "-p",
        "--previous-bundle-location",
        action="store",
        type=Path,
        help=(
            "The location to store the latest bundle. If left empty it will be the same as the created bundle. This is "
            "the reference point for creating incremental backups."
        ),
    )
    backup_parser.add_argument(
        "-m",
        "--metadata",
        action="store",
        type=Path,
        help=(
            "Additional metadata about the previous backup is read from this file, and data about this backup is "
            "written to it. Use of the metadata file allows additional functionality. In particular tags will only be "
            "backed up if a metadata file is used."
        ),
    )
    backup_parser.add_argument(
        "-M",
        "--mirror",
        action="store_true",
        help=(
            "Assume the repository to have been cloned in mirror mode. When performing a backup in mirror mode, a git "
            "remote update will first be triggered."
        ),
    )
    backup_parser.add_argument(
        "-t",
        "--timestamp",
        action="store_true",
        help="Add a timestamp (with second resolution) to the name of the created bundle file.",
    )
    backup_parser.add_argument(
        "-s",
        "--skip-unchanged",
        action="store_true",
        help=(
            "Do not create a bundle if it would contain no (incremental) changes. A bundle with changes to "
            "references but no new commits will still be created, and no warning will be emitted in that case."
        ),
    )

    restore_parser.add_argument(
        "repo", action="store", type=Path, default=Path(), help="The repository to restore data to."
    )
    restore_parser.add_argument(
        "bundle",
        action="store",
        type=Path,
        default=Path("backup.bundle"),
        help="The bundle file to restore. May be a directory containing bundle files (*.bundle).",
    )
    restore_parser.add_argument(
        "-b", "--bare", action="store_true", help="If a repository is created to restore to, make it a bare repository."
    )
    restore_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help=(
            "Force updates to all branches, even if the update is not a fast-forward or your worktree is not clean. If "
            "a file is provided as the bundle to restore (as opposed to a directory) then all references will be "
            "updated to reflect the contents of that bundle, even if the bundle does not contain any new commits."
        ),
    )
    restore_parser.add_argument(
        "-s",
        "--strict-order",
        action="store_true",
        help=(
            "If a directory is provided with bundles to restore, then the bundles in that directory are processsed "
            "strictly in order of their filenames. If a bundle fails to be restored, then no attempts will be made to "
            "restore other bundles from the directory. If both --strict-order and --force are used, then --force "
            "applies to each bundle in the directory as if the bundle file was being provided directly."
        ),
    )
    restore_parser.add_argument(
        "-p", "--prune", action="store_true", help="Remove any branches that are not in the bundle."
    )
    restore_parser.add_argument(
        "-d", "--delete-files", action="store_true", help="Delete bundle from which data has been restored."
    )
    restore_parser.add_argument(
        "-l",
        "--lock-file",
        action="store",
        type=Path,
        help=(
            "Create a lock file while restoring files. If the lock file already exists, exit with exit code 0, instead."
        ),
    )

    _add_common_arguments(backup_parser)
    _add_common_arguments(restore_parser)

    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, argparse.ArgumentTypeError) as exc:
        raise ExitWithExitCodeError(ExitCode.ARGPARSE_ERROR, str(exc)) from exc

    configure_logging(args)

    action = Actions(cast(str, args.action))

    log.debug("%s called with arguments: %s", __name__, argv)

    if action == Actions.Create:
        bundle = cast(Path, args.bundle)
        backup = Backup(
            cast(Path, args.repo),
            cast(str | None, args.remote),
            mirror=bool(args.mirror),
        )
        if backup.perform_backup(
            bundle,
            cast(Path | None, args.previous_bundle_location) or bundle,
            cast(Path | None, args.metadata),
            timestamped=bool(args.timestamp),
            skip_unchanged=bool(args.skip_unchanged),
        ):
            log.info("Created backup bundle %s", bundle)
    elif action == Actions.Restore:
        lock_file = cast(Path | None, args.lock_file)
        try:
            with simple_lock_file(lock_file) if lock_file else nullcontext():
                bundle = cast(Path, args.bundle)
                restoration = Restoration(
                    cast(Path, args.repo),
                    bare=bool(args.bare),
                    force=bool(args.force),
                    prune=bool(args.prune),
                    delete_files=bool(args.delete_files),
                )
                bundle_count = restoration.restore_bundles(bundle, strict_order=bool(args.strict_order))

                if restoration.restored_bundle_count == 0:
                    log.error("Restoring bundling to repository failed: no bundles were restored")
                    raise NoBundlesRestoredError

                if restoration.restored_bundle_count != bundle_count:
                    log.warning("%d bundles could not be restored.", bundle_count - restoration.restored_bundle_count)

                log.info("Restored %s bundles", restoration.restored_bundle_count)
        except SimpleLockFileNotCreatedError:
            log.warning("Could not obtain lock file %s. Not restoring anything.", lock_file)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ExitWithExitCodeError as exit_exc:
        if exit_exc.message is not None:
            # Print is the only viable way to inform the user, since we can't even rely on logging to have been
            # initialized
            print(exit_exc.message, file=sys.stderr)  # noqa: T201
        sys.exit(exit_exc.exit_code.value)
    except Exception:  # noqa: BLE001  # This is the termination point of all exceptions.
        # Only import traceback now that we really need it. No need to burden normal usage with that.
        from traceback import print_exc

        print_exc(file=sys.stderr)
        sys.exit(ExitCode.EXCEPTION_OCCURRED)
