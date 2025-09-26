# Incremental Git Backups Using Git Bundle

<!--
    Copyright (c)  2025  Thomas Schaap
    Permission is granted to copy, distribute and/or modify this document
    under the terms of the GNU Free Documentation License, Version 1.3
    or any later version published by the Free Software Foundation;
    with no Invariant Sections, no Front-Cover Texts, and no Back-Cover Texts.
    A copy of the license is included in the section entitled "GNU
    Free Documentation License".

    SPDX-License-Identifier: GFDL-1.3-or-later
-->

The `backup_bundle.py` script assists in forwarding incremental backups of git repositories between two machines that
are not connected (or at least: too little for git to work its own magic). It allows you to create an incremental
backup, which is just one file, of a source repository at one machine, somehow transport that file to the other machine,
and then restore that incremental backup in a target repository there.

In particular, this will provide (near) full synchronization of the target repository with the source repository, rather
than only synchronizing some handpicked branches. If you just need the handpicked branches and have full control over
both repositories, then plain (git bundle)[https://git-scm.com/docs/git-bundle] might already have you covered.

Although `backup_bundle.py` works on git repositories, it may still be of help if the usage scenarios matches your
situation but the incoming data is not a git repository yet: if it's easy to retrieve (updates to) your (external) data
into your source machine but hard to get them to the (disconnected) target machine, then you could still benefit from
this script's convenience by putting that data in a local git repository on your source machine.

# Usage & Scenarios

General usage of `backup_bundle.py` is pretty straightforward:

1. `python3 backup_bundle.py create source_repo backup.bundle`
2. `python3 backup_bundle.py restore target_repo backup.bundle`

And that's all you need to have a basic (incremental) backup made and restored.

There are several additional parameters that can be passed on the command line, which are detailed in the (command line
parameters)[#cmd-params] section.

To illustrate the usage of `backup_bundle.py` in real life, picture a medical research facility. A researcher at this
facility - that is: you - needs to analyze loads of confidential patient data, preferably with the best tools that are
out there.

To keep the confidential patient data safe, you are only allowed to process this in an internal network. That internal
network is air-gapped, or perhaps only connect with the internet using a guaranteed unidirectional connection. There
probably *are* ways to get information out - it would be a shame if your research yielded a cure for cancer but you're
not allowed to tell the world - but it'd be pretty expensive. Think about bureaucracy, hours of scrutinizing data to
make sure there's no confidential patient data there, perhaps some external auditors being involved, and so on.

At the same time there is a normal office network that is just fully internet connected, from where you can also send
data into the internal network. Via this network you can access your favourite tools on github and copy them to the
internal network, and here you can also develop your own public tooling that you still want to use inside the internal
network.

Obviously all these tools and external projects are hosted in git repositories.

## Workflow: Keep Up With Upstream

Let's say you're using an upstream project, but you have your own branch in the internal network with tweaks or little
extensions specific for your research. You won't be sharing this with the world, but you do want to keep up with the
awesome work that is being done upstream.

On your office network you set up a clone of the upstream repository:

    git clone https://github.com/awesome_project.git

Whenever you want to pull in updates from upstream, you first update your connected clone and create an incremental
backup bundle:

    cd /path/to/awesome_project
    git fetch --all --prune
    git pull
    cd /path/to/backup_directory
    python3 backup_bundle.py create /path/to/awesome_project incremental_backup.bundle --metadata metadata.json

Copy `incremental_backup.bundle` to the target machine (do not delete it from the source machine, it's used as reference
for the next incremental backup), then restore it:

    python3 backup_bundle.py restore /path/to/awesome_project_clone incremental_backup.bundle

Then merge or rebase your own branch to take advantage of the updates. For example:

    cd /path/to/awesome_project_clone
    git rebase main  # This assumes you originally branched off from main

This way of working assumes you create a new branch after the initial clone (which `backup_bundle.py` will do for you!)
where you keep your own changes.

Note that the backup creation part can be a one-liner if you use the `--mirror` and `--remote` options. This can be very
convenient if you don't want to use the source repository for something else than passing on these changes.

## Workflow: Rapid Development and Testing

This time, you're working on your own tools. You develop them on the office network, because there is no need to have
them in the internal network or perhaps because you want to use them there, as well. That does mean that when you need
to tweak your tools to work with your data on the internal network things can become a bit complicated, especially when
you want to have a quick back-and-forth between coding and testing.

Nothing is going to beat having all your code locally available where you want to develop it. But with the incremental
backups from `backup_bundle.py` a workflow can be created where you code on the office network and test in the internal
network. With a bit of practice it's a matter of seconds - plus any time it takes you to get from one machine to the
other.

On your office network you set up your repository as you normally would and you develop there. When you're working on a
feature or a tweak, you do so in a separate branch:

    cd /path/to/your_tool
    git switch -c work_for_internal_network

When you make changes to push to the internal network, make sure to commit them first:

    # Change code, update public datasets, tweak variables, write frustrated comments, etc, etc
    git add .  # This assumes you want to add just everything. Familiarize yourself with `git add` if you haven't, yet.
    git commit -m "Yet another update for the internal network"

Then you can use an incremental backup to push your changes to the internal network:

    python3 backup_bundle.py create /path/to/your_tool incremental_backup.bundle

Copy the `incremental_backup.bundle` to the internal network (do not delete it from the source machine, it's used as
reference for the next incremental backup), then restore it into your local copy:

    python3 backup_bundle.py restore /path/to/your_tool_clone incremental_backup.bundle

This workflow assumes that you checkout whichever branch you need on the internal network (presumably
`work_for_internal_network` in the above example) but you never make any changes there. A dual workflow is possible,
combining this with the first scenario, but you should familiarize yourself with `backup_bundle.py` before attempting
that to prevent misunderstandings that could loose you some work - you would probably need to use some `--force` for
that.

Note that if you can script the backup creation step and then copying the bundle to the internal network, then you can
turn this generic workflow into just one tailored command on each network.

## Workflow: Repository Mirroring

Instead of working on your local copies, sometimes you just need to make a repository available on your internal
network. Then anyone who can reach that internal copy of the repository can use it as an upstream source.

On the office network create backups based on a local mirror:

    mkdir -p /path/to/local_mirror_directory/updates  # First-time setup, only
    cd /path/to/local_mirror_directory
    python3 backup_bundle.py create repo updates/bundle.bundle --remote https://github.com/awesome_project.git \
        --mirror --metadata metadata.json --previous-bundle-location previous.bundle --timestamp --skip-unchanged

Copy the contents of the directory `/path/to/local_mirror_directory/updates/` to the internal network (make sure the
bundles have not been corrupted!). There you can restore them:

    python3 backup_bundle.py restore /path/to/awesome_project.git /path/to/updates/ --bare --force --prune \
        --delete-files --lock /path/to/lock_file

This will create a bare repository that mirrors the upstream repository. It can be used (read-only!) as if it were the
upstream repository itself.

Note that the backup step above will create a new bundle file every time there are changes. The restore command restores
any of those bundles it can, in the correct order. This is based on the fact that every consecutive bundle contains at
least one new commit - any form of timestamps is not used for this. If a bundle contains no new commits it's effectively
treated as if it was already restored and is just deleted.

This workflow allows decoupling the timing of creating backup bundles and restoring them. It also takes unreliable
transport into account in that missing files are not a problem, but they should not be corrupted.

If you have a more reliable transport mechanism, you can consider adding `--strict-order` on the internal network: this
will also restore any bundles that only contain reference updates (e.g. a new tag) without any new commits. These are
normally skipped because their ordering can't be determined automatically. This introduces a risk (very small, given the
rather odd upstream interactions that could trigger the situation) that when one or more bundles fail to be transported
correctly and a later bundle with only reference updates does transport correctly, that the copy on the internal network
ends up with its references in an odd state. This situation will always resolve itself again when a newer incremental
bundle is restored which contains at least one new commit.

## Exit Code

`backup_bundle.py` exits with one of the following return codes:

- 0
  - Success.
- 2
  - An error occurred while parsing the command line arguments.
- 3
  - No bundles were restored during a restore operation.
- 4
  - A backup from a non-existing repository was requested without specifying a remote repository to clone from.
- 5
  - A communication error with git occurred (e.g. git gave an unexpected response to a command).
- 6
  - A call to git failed unexpectedly.
- 100
  - An unexpected exception occurred. The exception will be printed to `stderr`.

Additionally `backup_bundle.py` can be terminated due to an exception being raised. This would also cause a non-zero
exit code.

If `backup_bundle.py` exits with a non-zero exit code, it will usually provide an indication as to why this occurred.
Except for exit code 2 and termination due to an exception, this explanation will be logged just like all other output
from `backup_bundle.py`. If this does not provide enough information, rerunning the same command with `-v` will provide
additional logging.

## Caveats

Incremental updates using git bundle files are nice and small, but due to technical limitations there are a number of
constraints.

### Branches Without Additional Commits {#empty-branches}

An old branch that does not have any commits of its own will increase the size of the incremental backups over time.

                              E -- F -- G branch                   E -- F -- G branch
                             /                                    /
    A -- B main        A -- B -- C -- D -- H main           A -- B -- C -- D -- H -- I -- J -- K -- L -- M -- N main
                                 ^                                    ^
                                 old_branch                           old_branch

Take the examples above. A first backup is made in the leftmost state. It contains commits `[A, B]`. An incremental
backup is then made of the middle state (incremental to the leftmost state). This incremental backup contains the new
commits `[C, D, E, F, G, H]`. But when an incremental backup is made of the rightmost state (incremental to the middle
state), then that not only contains the new commits `[I, J, K, L, M, N]` but also the already existing commits
`[C, D, H]`. This is required to include the reference to `old_branch`, which refers to `C`. Now consider that `main`
develops for another 1000 commits. Even if incremental backups are made after each of those commits, the newest of those
still contains over a 1000 commits (of which only 1 is new) in order to still include `old_branch`.

A branch like `old_branch` above, which is created but never actually *branches off* with a new commit, will continue to
be included and force the bundle to include its commit. This will not break anything, but it *is* less efficient.

### Tags

Tags are only included with the `--metadata` option when creating a backup bundle. More importantly, tags will never be
changed after they have been included once. This is in line with
(git's own policy on tags)[https://git-scm.com/docs/git-tag#_on_re_tagging]: once a tag is laid, it's considered final.

If, at any point, you *do* need to update a tag that has already been copied via a backup bundle, it's probably best to
just do it manually. Alternatively, you can remove the tag from the metadata file and create a new backup bundle. Then
either remove the tag from the target repository before restoring it, or use `--force` when restoring it.

If you create an incremental backup bundle but include tags for the first time, you will find that this bundle is
surprisingly large. Similarly, new tags that are laid somewhat deeper in the repository's history can cause an
incremental backup to become larger than expected. This effect will go away again with the next incremental backup.

### Using `--force`

Although `--force` sounds like a solution to all your problems, it should be used with care. While both
`backup_bundle.py` and git try hard not to touch your work, using `--force` will make them both a lot more aggressive.
You're basically saying: "I don't care what you have to do, make it work." And *they will*.

In a bare target repository using `--force` is not really an issue. You're presumably not actively working there,
anyway, so all you're doing is making sure changes are made so the target repository continues to closely mirror the
source repository.

Using `--force` in a normal repository, however, is a lot more hazardous. If you have local changes those might be gone.
You might end up in a state that is rather unintuitive and if you're not being very sharp you will end up losing work or
creating a broken state.

If you have to use `--force` in a normal repository, it is best to make sure your work is safe. Commit it, or at the
very least stash it. Having it staged for committing is *not* enough. And when you've done that, you should first try
again without `--force` - especially `backup_bundle.py` tries very hard to do the right thing, as long as your worktree
is clean. Chances are all you'll need to do then is rebase or merge your own work on the successfully restored updates.

# Command Line Parameters {#cmd-params}

The two general forms of calling `backup_bundle.py` are:

1. `python3 backup_bundle.py create REPO BUNDLE [--remote REMOTE] [--mirror] [--skip-unchanged] [--previous-bundle-location PREVIOUS] [--metadata METADATA]`
2. `python3 backup_bundle.py restore REPO BUNDLE [--bare] [--strict-order] [--force] [--prune] [--delete-files] [--lock-file LOCK_FILE]`

Additionally both forms accept logging configuration:

- `[--verbose] [--log-config-file LOG_CONFIG_FILE] [--log-config LOG_CONFIG]`

Finally, `--help` can always be given to obtain additional information about the configuration options.

## Create a Backup Bundle

The first form will create a new backup bundle `BUNDLE` from git repository `REPO`.

### `-r`, `--remote`

If `REPO` does not exist yet or is an empty directory, then a clone of `REMOTE` will be made.

If `--mirror` is also given the clone will be made with `git clone`'s `--mirror`, resulting in a bare repository.
Otherwise a normal clone is created, but without a checkout. If a worktree is desired, you can run, for example,
`git switch main` to create a checkout (of `main`, in this case) later on.

The option has no effect if `REPO` is already an existing git repository.

### `-M`, `--mirror`

In mirroring mode, `REPO` is assumed to be a bare repository. If a `--remote` is given, the repository will be cloned
with `git clone`'s `--mirror`, making it a bare repository that extremely closely tracks the remote repository.

When creating a backup bundle with `--mirror`, a `git remote update` will be performed first to pull in the latests
changes from upstream.

### `-s`, `--skip-unchanged`

Normally `backup-bundle.py` will always create a new backup bundle, even if there were no actual changes. With
`--skip-unchanged` no file will be written if there are no changes at all. Additionally, no warning will be given if
the new backup bundle would only contain reference updates but no new commits.

`--skip-unchanged` is geared towards automated mirroring and pairs well with `--force` and `--strict-order`, provided
that the (automated) transport is sufficiently reliable.

### `-p`, `--previous-bundle-location`

Incremental backups are created by using the backup bundle from the previous (incremental) backup as a reference point.
By default this reference is just `BUNDLE` itself, but it can be explicitly set to a different file.

This option is particularly useful when using `--timestamp` to create (a directory of) timestamped bundle files.
Although `--timestamp` will work without `--previous-bundle-location`, `BUNDLE` would then still be written and in the
same directory as the timestamped bundles.

If the previous bundle is not present, then `backup_bundle.py` will just create a full backup ('incremental' compared to
nothing). It does not matter whether the previous bundle is explicitly set to `PREVIOUS` or was implied to be `BUNDLE`.

### `-m`, `--metadata`

Use an additional (JSON-encoded) state file `METADATA` as a reference point for incremental files.

Due to a limitation in git bundles, it is not viable to include all tags in every incremental backup bundle. Because of
this the previous backup bundle does not, in fact, provide a correct reference point for determining which tags to
include in the next incremental update. Tags will only be replicated if `--metadata` is set.

If `METADATA` does not exist yet, it is presumed to be empty.

## Restore Backup Bundles

The second form will restore data from backup bundles to git repository `REPO`. If `BUNDLE` is a file, then that single
file is used. If `BUNDLE` is a directory, then all files matching glob `*.bundle` in the directory `BUNDLE` are used.

Restoring bundles from a directory is done bundle by bundle. Whenever a bundle is successfully restored all other
bundles will be attempted again, so as long as all necessary bundles are in the directory they will all be restored.
For optimization purposes the bundles in a directory are ordered by their filename, which is optimal for a directory of
bundles created with the `--timestamp` option. This behavior can be altered with the `--strict-order` option.

If `REPO` does not exist yet, it is created and intialized as a git repository.

### `-b`, `--bare`

If `REPO` has to be created by `backup_bundle.py`, then it will be created as a bare repository. Otherwise a normal
repository will be created.

This option has no effect if `REPO` is an existing git repository.

### `-s`, `--strict-order`

If `BUNDLE` is a directory, then the backup bundles in that directory are processed strictly in order of their
filenames. If one bundle fails to be restored, then no attempts will be made to restore any of the other bundles.

Because `--strict-order` provides guarantees to `backup-bundle.py` that the backup bundles in `BUNDLE` are correctly
ordered, combining it with `--force` also convinces `backup-bundle.py` that they are up to date. As such, backup bundles
without new commits will *not* be skipped, but have their reference updates restored to `REPO`.

`--strict-order` is geared towards automated mirroring and assumes sufficiently reliable (automated) transport of the
backup bundles, especially when using `--force`. Using `--timestamp` for creating the backup bundles is a natural
combination, and it pairs well with `--skip-unchanged`.

### `-f`, `--force`

Forcibly update `REPO`. This will allow:

- Updates to references that are not fast-forward;
- Updates to the currently checked out branch while the worktree is not clean;
- Removing the currently checked out branch with `--prune`.

Additionally, if `BUNDLE` contains no new commits and is either a single file or `--strict-order` is given, then it will
still be restored in that all the references in `REPO` are updated to those in `BUNDLE`. Without `--force` a bundle with
no new commits will be ignored, even if the references it has are different from those in `REPO`, because
`backup_bundle.py` can't decide whether it's newer or older than the information in `REPO`. Using `--force` convinces
`backup_bundle.py` that, yes, `BUNDLE` *is* the newest version.

### `-p`, `--prune`

Allow removing references from `REPO`. This more closely mirrors the source repository, but will also remove any
branches that only exist in the target repository (for example because the user made them there by hand).

It is not allowed to remove the currently checked out branch. `--force` can be used to override this. The repository
will be left in a detached HEAD state after that, allowing you to still recover.

### `-d`, `--delete-files`

If this option is set, `backup_bundle.py` will remove any bundle files it has successfully restored. This is
particularly useful when `BUNDLE` is a directory.

This option will also consider bundle files with no new commits as having been successfully restored, which makes a
difference for the exit code if only such bundle files were found.

### `-l`, `--lock-file`

Create the file `LOCK_FILE` using exclusive create mode to indicate taking a lock. If this file already exists, then
the lock was presumably taken by another process and no restoring will be done; the exit code will be 0.

## Logging Configuration

All output generated by `backup_bundle.py` is generated through Python's
(logging)[https://docs.python.org/3/library/logging.html] module. Normal messages are logged as `INFO` level (and above
where applicable), verbose logging is done at the `DEBUG` level. The basic configuration is to simply print messages to
`stdout`.

### `-v`, `--verbose`

Provide additional debug logging. This optional is very useful if `backup_bundle.py` fails or refuses your request and
you wish to understand what is going on behind the scenes.

### `--log-config-file`

Provide a configuration for Python logging. The contents of `LOG_CONFIG_FILE` will be read and interpreted as JSON,
yielding a `dict` that will be passed to
(`logging.config.configDict`)[https://docs.python.org/3/library/logging.config.html#logging.config.dictConfig]
before the logger for `backup_bundle.py` is created.

### `--log-config`

Provide a configuration for Python logging. `LOG_CONFIG` will be interpreted as JSON, yielding a `dict` that will be
passed to (`logging.config.configDict`)[https://docs.python.org/3/library/logging.config.html#logging.config.dictConfig]
before the logger for `backup_bundle.py` is created.

# Under the Hood

`backup_bundle.py` is a wrapper around (git)[https://git-scm.com/] that basically enhances the functionality of
`git bundle` and streamlines that for the usage scenarios described above.

## git Bundles

(git bundles)[https://git-scm.com/docs/git-bundle] are basically a package of some commits with some metadata. See git's
own documentation for details.

### Non-consecutive Commit Sets

Although the underlying file format could probably handle it, `git bundle` does not allow creation of bundle files that
contain non-consecutive commits within the same branch. See the (caveat about branches without commits)[#empty-branches]
for the user-facing implications.

    A -- B -- C -- D -- E main
         ^         ^
         branch    refs/remotes/previous_backup/main

The problem stems from `git bundle` only including references for which it contains the commit, and working with a
single `git rev-list` to construct the set of commits to include. In the example above,
`refs/remotes/previous_backup/main` is the point where a previous backup was created. An incremental backup would be
complete if it just contains commit `E` and references `refs/heads/main -> E` and `refs/heads/branch -> B`. But
if we call `git bundle` to include `main` and `branch` (you can only include references with `git bundle`) but not
commit `D`, then `git rev-list` will conclude that we only want `E` (which is correct) and `git bundle` will conclude
that it doesn't need to include `refs/heads/branch` because `B` is not included (*woops*).

Now suppose we tell `git bundle` to include `main` and not commit `D` and `branch` and not commit `A`. Sounds good?
`git rev-list` simplifies this into "include `main` and `branch`, but not `D` and `A`", which gives the exact same
result as before.

To include `branch` we *must* include `B` and since `git rev-list` won't create a gap for us, we hence *must* include
all commits except `A`. And there you have the caveat.

This is also the exact reason that tags are not included by default. If we were to include all tags in every incremental
update, then we would need to include their commits as well. Just read `refs/tags/v_very_old` where it says
`refs/heads/branch` in the previous description and the problem should become clear.

### Limited Querying

Although `git bundle` seems to imply that a bundle file can be used as a read-only repository, it's important to realize
that it explicit states that it can be treated as a *remote* repository. This severely limits the amount of operations.
In particular we can query the references in the bundle and which commits they refer to, but *not* which other commits
are in the bundle.

It would strictly speaking be possible using `git bundle unbundle`, and then continuing with additional plumbing, this
is very low-level and explicitly warned against (and would effectively re-implement part of `git fetch`).

Not going that route does mean that we are somewhat limited in some checks and, going even further, in detecting whether
a forced restore would destroy work - hence the advise to clean out even untracked files from the worktree before
restoring. Without this limitation `backup_bundle.py` could resolve more situations by itself without resorting to
telling the user to fix it or use `--force`.

## Backup Algorithm

When creating an incremental backup (possibly against 'nothing') the algorithm below is used to determine the contents
of the backup bundle.

1. List all references of the form `refs/heads/*`
  - These must all be included
2. If tags are requested via `--metadata`:
  - List all references of the form `refs/tags/*`
  - Remove those that were already mentioned in the metadata file
  - These must all be included
3. If a previous backup bundle exists, collect the commits each of the references in the bundle points to
  - Otherwise this is an empty set
4. List all the *new* commits to be included in the bundle: these are all commit reachable from the references found in
   (1) and (2), with all the commits reachable from the commits found in (3) removed again
  - In other words: all commits that are in the current repository, but not in the previous backup bundle
  - These must be included
5. Collect all possible exclusion points:
  - Collect all commits found in (3)
  - Collect the parent commit of each reference found in (1) and (2)
6. Filter possible exclusion points
  - For every exclusion point:
    - If a commit found in (1), (2) or (4) is reachable from this exclusion point, then don't use it
  - This leaves the most stringent list of exclusions that does not remove any commit that must be included

The final result is adding all commits found in (1), (2) and (4), but excluding the result of (6).

## Restoration Algorithm

When restoring an incremental backup, the current state of the repository must be inspected and compared against the
incremental backup. Loss of data is to be avoided (even with `--force`, except as described with that option), but
refusing too easily is frustrating and would lead the user to use `--force` superfluously. The algorithm below balances
this for restoring a single bundle (multiple bundles is just doing them one by one).

1. Check if the commits referenced in the bundle already exist in the repository
  - If all of them already exist, then the bundle contains no new updates. It is skipped (but see the exception with
    `--force`)
2. Detect an update to the current HEAD (i.e. currently checked out branch)
  - In a bare repository: no problem
    - HEAD is rather meaningless in a bare repository, as there is no checkout
  - When using `--force`: no problem
  - Deleting the current HEAD without `--force`: **refused**
  - If the worktree is entirely clean: no problem
    - If the worktree is clean, we do no risk loss of data
  - An update to the current HEAD with an unclean worktree and no `--force`: **refused**
3. Verify the bundle against the repository with `git bundle verify`
  - If this fails, then the bundle can't be restored
4. Attempt a dry-run with `git fetch`
  - If this fails, then the bundle can't be restored
5. If the current HEAD points to a branch that would be updated to a new commit:
  - `git fetch --prefetch` the new reference from the bundle
  - Hard reset the local branch to that new reference
  - From this point on, we can treat the current HEAD as not being updated
6. If the current HEAD points to a branch that would be deleted
  - `git switch --detach` to detach the HEAD
  - From this point on, we can treat the current HEAD as not being touched
7. `git fetch` all the updates from the bundle
  - Use `--update-head-ok` if needed to convince git that the current HEAD may be updated. The above steps fix any
    objections git may have had against it (but it would refuse anyway).
  - Update *all* heads (refspec `refs/heads/*:refs/heads/*`); with `--prune` this will remove branches that are no
    longer listed in the bundle
  - Update the tags that are explicitly listed in the bundle's references

Most of the work is just done right by git. The difficulty is handling the updates to HEAD without user intervention,
which is what most steps are for. Normally git would have the user decide on the next step to resolve the issue, but for
`backup_bundle.py` we really want it to go right in one step - or not, and know about it.

# Development

Development on `backup_bundle.py` was done after deciding on the design principles listed below. To continue
development, please stick to those and read the sections on tools and testing.

## Design Principles

The following principles were leading when building `backup_bundle.py`, and remain valid for continued development:

- One script file
- Use a single git bundle
- No additional dependencies
- git-like restraint
- It just works

### One Script File

`backup_bundle.py` is essentially a (small?) shell script. People should not need to install packages just to run this
one shell script. That doesn't mean that should not be an option, but it *should* be an option to just take the one
script file, deploy it to where you need it and run it there.

Please note that the intent is not to just use the script file and discard everything else. In particular this README is
part of the full distribution. But a full distribution is not needed, for example, on a headless server that just
creates backup bundles. While you would have the full distribution available locally - and presumably somewhere
locatable for colleagues that also need to maintain that server - you can just deploy the one script to the server.

### Use a Single Git Bundle

Git bundles are a standardized format for transferring and restoring (partial) git repositories. Using a standardized
format is always a good idea. Additionally, in the type of environment `backup_bundle.py` is meant for it easily
becomes a hassle if something like an incremental backup consists of multiple files. You'd have to somehow synchronize
their delivery, or package them together but then also unpackage them again.

It has been considered to pack the git bundle together with some additional metadata into, for example, a tarball, but
the benefits were low and it would require another layer around the core functionality - because git does need the
bundle readily available on the filesystem.

### No Additional Dependencies

Like with the one script file principle: one should not need all kinds of additional packages to run a simple shell
script.

Optional dependencies that introduce end user convenience would be acceptable (`requests` to allow backing up to and
restoring from remote URIs, for example?).

### git-like Restraint

Do Not Cause Loss Of Data.

It's a very good principle, especially for a source control tool. As `backup_bundle.py` can be seen as an extension to
git itself, it should stick to this principle.

### It Just Works

End-user experience is important. Just about everything `backup_bundle.py` does is possible with plain old git. But if
you read the steps to take, it feels barebones. Streamlining all that is the reason `backup_bundle.py` exists, so it
should make good on that promise of being streamlined.

## Tools

During development the following tools should be used:

- mypy
- ruff
- pytest

### mypy

Although just run in its default configuration, mypy (or a similar static typing analyzer) should be used to verify
typing correctness.

### ruff

Ruff was used in both its checking and formatting mode. A configuration is available with the source of
`backup_bundle.py`. At the time of writing `--preview` was used with ruff, as well, but that is probably not stable
enough to require in the long run. Still, if it works or gives good results, go for it.

### pytest

Tests are based on the pytest framework. Be sure to point pytest directly at the `backup_bundle_tests.py` file.

### All Together

No project management tooling is used. Just use the following steps - script them if you want:

    # Initial setup
    python3 -m venv venv
    ./venv/bin/pip install mypy ruff pytest

    # Check and test code
    ./venv/bin/mypy .
    ./venv/bin/ruff check .  # Might want to --preview here. There's a noqa annotation for a preview rule (currently)
    ./venv/bin/ruff format --check .
    ./venv/bin/pytest backup_bundle_tests.py

## Testing

`backup_bundle.py` is intended for use in production environments. This means that people will use it for their daily
work and depend on its correct functioning. At the same time `backup_bundle.py` works with git repositories and git
repositories can come in many different flavors and states.

This means that testing is very important! When implementing or changing features in `backup_bundle.py`, adding test
scenarios to cover your feature and ensure that it works as intended in all foreseeable cases, is imperative.

Similarly, when a test no longer runs due to changes, consider, reconsider and then reconsider again whether the test's
modelled and expected behavior is indeed no longer the desired behavior. Because it probably still is, and just "fixing"
the test then introduces incorrect behavior into the script and hence into people's daily work.

# GNU Free Documentation License

```
                GNU Free Documentation License
                 Version 1.3, 3 November 2008


 Copyright (C) 2000, 2001, 2002, 2007, 2008 Free Software Foundation, Inc.
     <https://fsf.org/>
 Everyone is permitted to copy and distribute verbatim copies
 of this license document, but changing it is not allowed.

0. PREAMBLE

The purpose of this License is to make a manual, textbook, or other
functional and useful document "free" in the sense of freedom: to
assure everyone the effective freedom to copy and redistribute it,
with or without modifying it, either commercially or noncommercially.
Secondarily, this License preserves for the author and publisher a way
to get credit for their work, while not being considered responsible
for modifications made by others.

This License is a kind of "copyleft", which means that derivative
works of the document must themselves be free in the same sense.  It
complements the GNU General Public License, which is a copyleft
license designed for free software.

We have designed this License in order to use it for manuals for free
software, because free software needs free documentation: a free
program should come with manuals providing the same freedoms that the
software does.  But this License is not limited to software manuals;
it can be used for any textual work, regardless of subject matter or
whether it is published as a printed book.  We recommend this License
principally for works whose purpose is instruction or reference.


1. APPLICABILITY AND DEFINITIONS

This License applies to any manual or other work, in any medium, that
contains a notice placed by the copyright holder saying it can be
distributed under the terms of this License.  Such a notice grants a
world-wide, royalty-free license, unlimited in duration, to use that
work under the conditions stated herein.  The "Document", below,
refers to any such manual or work.  Any member of the public is a
licensee, and is addressed as "you".  You accept the license if you
copy, modify or distribute the work in a way requiring permission
under copyright law.

A "Modified Version" of the Document means any work containing the
Document or a portion of it, either copied verbatim, or with
modifications and/or translated into another language.

A "Secondary Section" is a named appendix or a front-matter section of
the Document that deals exclusively with the relationship of the
publishers or authors of the Document to the Document's overall
subject (or to related matters) and contains nothing that could fall
directly within that overall subject.  (Thus, if the Document is in
part a textbook of mathematics, a Secondary Section may not explain
any mathematics.)  The relationship could be a matter of historical
connection with the subject or with related matters, or of legal,
commercial, philosophical, ethical or political position regarding
them.

The "Invariant Sections" are certain Secondary Sections whose titles
are designated, as being those of Invariant Sections, in the notice
that says that the Document is released under this License.  If a
section does not fit the above definition of Secondary then it is not
allowed to be designated as Invariant.  The Document may contain zero
Invariant Sections.  If the Document does not identify any Invariant
Sections then there are none.

The "Cover Texts" are certain short passages of text that are listed,
as Front-Cover Texts or Back-Cover Texts, in the notice that says that
the Document is released under this License.  A Front-Cover Text may
be at most 5 words, and a Back-Cover Text may be at most 25 words.

A "Transparent" copy of the Document means a machine-readable copy,
represented in a format whose specification is available to the
general public, that is suitable for revising the document
straightforwardly with generic text editors or (for images composed of
pixels) generic paint programs or (for drawings) some widely available
drawing editor, and that is suitable for input to text formatters or
for automatic translation to a variety of formats suitable for input
to text formatters.  A copy made in an otherwise Transparent file
format whose markup, or absence of markup, has been arranged to thwart
or discourage subsequent modification by readers is not Transparent.
An image format is not Transparent if used for any substantial amount
of text.  A copy that is not "Transparent" is called "Opaque".

Examples of suitable formats for Transparent copies include plain
ASCII without markup, Texinfo input format, LaTeX input format, SGML
or XML using a publicly available DTD, and standard-conforming simple
HTML, PostScript or PDF designed for human modification.  Examples of
transparent image formats include PNG, XCF and JPG.  Opaque formats
include proprietary formats that can be read and edited only by
proprietary word processors, SGML or XML for which the DTD and/or
processing tools are not generally available, and the
machine-generated HTML, PostScript or PDF produced by some word
processors for output purposes only.

The "Title Page" means, for a printed book, the title page itself,
plus such following pages as are needed to hold, legibly, the material
this License requires to appear in the title page.  For works in
formats which do not have any title page as such, "Title Page" means
the text near the most prominent appearance of the work's title,
preceding the beginning of the body of the text.

The "publisher" means any person or entity that distributes copies of
the Document to the public.

A section "Entitled XYZ" means a named subunit of the Document whose
title either is precisely XYZ or contains XYZ in parentheses following
text that translates XYZ in another language.  (Here XYZ stands for a
specific section name mentioned below, such as "Acknowledgements",
"Dedications", "Endorsements", or "History".)  To "Preserve the Title"
of such a section when you modify the Document means that it remains a
section "Entitled XYZ" according to this definition.

The Document may include Warranty Disclaimers next to the notice which
states that this License applies to the Document.  These Warranty
Disclaimers are considered to be included by reference in this
License, but only as regards disclaiming warranties: any other
implication that these Warranty Disclaimers may have is void and has
no effect on the meaning of this License.

2. VERBATIM COPYING

You may copy and distribute the Document in any medium, either
commercially or noncommercially, provided that this License, the
copyright notices, and the license notice saying this License applies
to the Document are reproduced in all copies, and that you add no
other conditions whatsoever to those of this License.  You may not use
technical measures to obstruct or control the reading or further
copying of the copies you make or distribute.  However, you may accept
compensation in exchange for copies.  If you distribute a large enough
number of copies you must also follow the conditions in section 3.

You may also lend copies, under the same conditions stated above, and
you may publicly display copies.


3. COPYING IN QUANTITY

If you publish printed copies (or copies in media that commonly have
printed covers) of the Document, numbering more than 100, and the
Document's license notice requires Cover Texts, you must enclose the
copies in covers that carry, clearly and legibly, all these Cover
Texts: Front-Cover Texts on the front cover, and Back-Cover Texts on
the back cover.  Both covers must also clearly and legibly identify
you as the publisher of these copies.  The front cover must present
the full title with all words of the title equally prominent and
visible.  You may add other material on the covers in addition.
Copying with changes limited to the covers, as long as they preserve
the title of the Document and satisfy these conditions, can be treated
as verbatim copying in other respects.

If the required texts for either cover are too voluminous to fit
legibly, you should put the first ones listed (as many as fit
reasonably) on the actual cover, and continue the rest onto adjacent
pages.

If you publish or distribute Opaque copies of the Document numbering
more than 100, you must either include a machine-readable Transparent
copy along with each Opaque copy, or state in or with each Opaque copy
a computer-network location from which the general network-using
public has access to download using public-standard network protocols
a complete Transparent copy of the Document, free of added material.
If you use the latter option, you must take reasonably prudent steps,
when you begin distribution of Opaque copies in quantity, to ensure
that this Transparent copy will remain thus accessible at the stated
location until at least one year after the last time you distribute an
Opaque copy (directly or through your agents or retailers) of that
edition to the public.

It is requested, but not required, that you contact the authors of the
Document well before redistributing any large number of copies, to
give them a chance to provide you with an updated version of the
Document.


4. MODIFICATIONS

You may copy and distribute a Modified Version of the Document under
the conditions of sections 2 and 3 above, provided that you release
the Modified Version under precisely this License, with the Modified
Version filling the role of the Document, thus licensing distribution
and modification of the Modified Version to whoever possesses a copy
of it.  In addition, you must do these things in the Modified Version:

A. Use in the Title Page (and on the covers, if any) a title distinct
   from that of the Document, and from those of previous versions
   (which should, if there were any, be listed in the History section
   of the Document).  You may use the same title as a previous version
   if the original publisher of that version gives permission.
B. List on the Title Page, as authors, one or more persons or entities
   responsible for authorship of the modifications in the Modified
   Version, together with at least five of the principal authors of the
   Document (all of its principal authors, if it has fewer than five),
   unless they release you from this requirement.
C. State on the Title page the name of the publisher of the
   Modified Version, as the publisher.
D. Preserve all the copyright notices of the Document.
E. Add an appropriate copyright notice for your modifications
   adjacent to the other copyright notices.
F. Include, immediately after the copyright notices, a license notice
   giving the public permission to use the Modified Version under the
   terms of this License, in the form shown in the Addendum below.
G. Preserve in that license notice the full lists of Invariant Sections
   and required Cover Texts given in the Document's license notice.
H. Include an unaltered copy of this License.
I. Preserve the section Entitled "History", Preserve its Title, and add
   to it an item stating at least the title, year, new authors, and
   publisher of the Modified Version as given on the Title Page.  If
   there is no section Entitled "History" in the Document, create one
   stating the title, year, authors, and publisher of the Document as
   given on its Title Page, then add an item describing the Modified
   Version as stated in the previous sentence.
J. Preserve the network location, if any, given in the Document for
   public access to a Transparent copy of the Document, and likewise
   the network locations given in the Document for previous versions
   it was based on.  These may be placed in the "History" section.
   You may omit a network location for a work that was published at
   least four years before the Document itself, or if the original
   publisher of the version it refers to gives permission.
K. For any section Entitled "Acknowledgements" or "Dedications",
   Preserve the Title of the section, and preserve in the section all
   the substance and tone of each of the contributor acknowledgements
   and/or dedications given therein.
L. Preserve all the Invariant Sections of the Document,
   unaltered in their text and in their titles.  Section numbers
   or the equivalent are not considered part of the section titles.
M. Delete any section Entitled "Endorsements".  Such a section
   may not be included in the Modified Version.
N. Do not retitle any existing section to be Entitled "Endorsements"
   or to conflict in title with any Invariant Section.
O. Preserve any Warranty Disclaimers.

If the Modified Version includes new front-matter sections or
appendices that qualify as Secondary Sections and contain no material
copied from the Document, you may at your option designate some or all
of these sections as invariant.  To do this, add their titles to the
list of Invariant Sections in the Modified Version's license notice.
These titles must be distinct from any other section titles.

You may add a section Entitled "Endorsements", provided it contains
nothing but endorsements of your Modified Version by various
parties--for example, statements of peer review or that the text has
been approved by an organization as the authoritative definition of a
standard.

You may add a passage of up to five words as a Front-Cover Text, and a
passage of up to 25 words as a Back-Cover Text, to the end of the list
of Cover Texts in the Modified Version.  Only one passage of
Front-Cover Text and one of Back-Cover Text may be added by (or
through arrangements made by) any one entity.  If the Document already
includes a cover text for the same cover, previously added by you or
by arrangement made by the same entity you are acting on behalf of,
you may not add another; but you may replace the old one, on explicit
permission from the previous publisher that added the old one.

The author(s) and publisher(s) of the Document do not by this License
give permission to use their names for publicity for or to assert or
imply endorsement of any Modified Version.


5. COMBINING DOCUMENTS

You may combine the Document with other documents released under this
License, under the terms defined in section 4 above for modified
versions, provided that you include in the combination all of the
Invariant Sections of all of the original documents, unmodified, and
list them all as Invariant Sections of your combined work in its
license notice, and that you preserve all their Warranty Disclaimers.

The combined work need only contain one copy of this License, and
multiple identical Invariant Sections may be replaced with a single
copy.  If there are multiple Invariant Sections with the same name but
different contents, make the title of each such section unique by
adding at the end of it, in parentheses, the name of the original
author or publisher of that section if known, or else a unique number.
Make the same adjustment to the section titles in the list of
Invariant Sections in the license notice of the combined work.

In the combination, you must combine any sections Entitled "History"
in the various original documents, forming one section Entitled
"History"; likewise combine any sections Entitled "Acknowledgements",
and any sections Entitled "Dedications".  You must delete all sections
Entitled "Endorsements".


6. COLLECTIONS OF DOCUMENTS

You may make a collection consisting of the Document and other
documents released under this License, and replace the individual
copies of this License in the various documents with a single copy
that is included in the collection, provided that you follow the rules
of this License for verbatim copying of each of the documents in all
other respects.

You may extract a single document from such a collection, and
distribute it individually under this License, provided you insert a
copy of this License into the extracted document, and follow this
License in all other respects regarding verbatim copying of that
document.


7. AGGREGATION WITH INDEPENDENT WORKS

A compilation of the Document or its derivatives with other separate
and independent documents or works, in or on a volume of a storage or
distribution medium, is called an "aggregate" if the copyright
resulting from the compilation is not used to limit the legal rights
of the compilation's users beyond what the individual works permit.
When the Document is included in an aggregate, this License does not
apply to the other works in the aggregate which are not themselves
derivative works of the Document.

If the Cover Text requirement of section 3 is applicable to these
copies of the Document, then if the Document is less than one half of
the entire aggregate, the Document's Cover Texts may be placed on
covers that bracket the Document within the aggregate, or the
electronic equivalent of covers if the Document is in electronic form.
Otherwise they must appear on printed covers that bracket the whole
aggregate.


8. TRANSLATION

Translation is considered a kind of modification, so you may
distribute translations of the Document under the terms of section 4.
Replacing Invariant Sections with translations requires special
permission from their copyright holders, but you may include
translations of some or all Invariant Sections in addition to the
original versions of these Invariant Sections.  You may include a
translation of this License, and all the license notices in the
Document, and any Warranty Disclaimers, provided that you also include
the original English version of this License and the original versions
of those notices and disclaimers.  In case of a disagreement between
the translation and the original version of this License or a notice
or disclaimer, the original version will prevail.

If a section in the Document is Entitled "Acknowledgements",
"Dedications", or "History", the requirement (section 4) to Preserve
its Title (section 1) will typically require changing the actual
title.


9. TERMINATION

You may not copy, modify, sublicense, or distribute the Document
except as expressly provided under this License.  Any attempt
otherwise to copy, modify, sublicense, or distribute it is void, and
will automatically terminate your rights under this License.

However, if you cease all violation of this License, then your license
from a particular copyright holder is reinstated (a) provisionally,
unless and until the copyright holder explicitly and finally
terminates your license, and (b) permanently, if the copyright holder
fails to notify you of the violation by some reasonable means prior to
60 days after the cessation.

Moreover, your license from a particular copyright holder is
reinstated permanently if the copyright holder notifies you of the
violation by some reasonable means, this is the first time you have
received notice of violation of this License (for any work) from that
copyright holder, and you cure the violation prior to 30 days after
your receipt of the notice.

Termination of your rights under this section does not terminate the
licenses of parties who have received copies or rights from you under
this License.  If your rights have been terminated and not permanently
reinstated, receipt of a copy of some or all of the same material does
not give you any rights to use it.


10. FUTURE REVISIONS OF THIS LICENSE

The Free Software Foundation may publish new, revised versions of the
GNU Free Documentation License from time to time.  Such new versions
will be similar in spirit to the present version, but may differ in
detail to address new problems or concerns.  See
https://www.gnu.org/licenses/.

Each version of the License is given a distinguishing version number.
If the Document specifies that a particular numbered version of this
License "or any later version" applies to it, you have the option of
following the terms and conditions either of that specified version or
of any later version that has been published (not as a draft) by the
Free Software Foundation.  If the Document does not specify a version
number of this License, you may choose any version ever published (not
as a draft) by the Free Software Foundation.  If the Document
specifies that a proxy can decide which future versions of this
License can be used, that proxy's public statement of acceptance of a
version permanently authorizes you to choose that version for the
Document.

11. RELICENSING

"Massive Multiauthor Collaboration Site" (or "MMC Site") means any
World Wide Web server that publishes copyrightable works and also
provides prominent facilities for anybody to edit those works.  A
public wiki that anybody can edit is an example of such a server.  A
"Massive Multiauthor Collaboration" (or "MMC") contained in the site
means any set of copyrightable works thus published on the MMC site.

"CC-BY-SA" means the Creative Commons Attribution-Share Alike 3.0
license published by Creative Commons Corporation, a not-for-profit
corporation with a principal place of business in San Francisco,
California, as well as future copyleft versions of that license
published by that same organization.

"Incorporate" means to publish or republish a Document, in whole or in
part, as part of another Document.

An MMC is "eligible for relicensing" if it is licensed under this
License, and if all works that were first published under this License
somewhere other than this MMC, and subsequently incorporated in whole or
in part into the MMC, (1) had no cover texts or invariant sections, and
(2) were thus incorporated prior to November 1, 2008.

The operator of an MMC Site may republish an MMC contained in the site
under CC-BY-SA on the same site at any time before August 1, 2009,
provided the MMC is eligible for relicensing.


ADDENDUM: How to use this License for your documents

To use this License in a document you have written, include a copy of
the License in the document and put the following copyright and
license notices just after the title page:

    Copyright (c)  YEAR  YOUR NAME.
    Permission is granted to copy, distribute and/or modify this document
    under the terms of the GNU Free Documentation License, Version 1.3
    or any later version published by the Free Software Foundation;
    with no Invariant Sections, no Front-Cover Texts, and no Back-Cover Texts.
    A copy of the license is included in the section entitled "GNU
    Free Documentation License".

If you have Invariant Sections, Front-Cover Texts and Back-Cover Texts,
replace the "with...Texts." line with this:

    with the Invariant Sections being LIST THEIR TITLES, with the
    Front-Cover Texts being LIST, and with the Back-Cover Texts being LIST.

If you have Invariant Sections without Cover Texts, or some other
combination of the three, merge those two alternatives to suit the
situation.

If your document contains nontrivial examples of program code, we
recommend releasing these examples in parallel under your choice of
free software license, such as the GNU General Public License,
to permit their use in free software.
```