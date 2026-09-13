import html
import os
import subprocess
import threading

import sublime
import sublime_plugin


GIT_TIMEOUT = 10

# Resolved against the active color scheme, so it tracks the user's theme.
DANGER = "var(--redish)"


class GitError(Exception):
    def __init__(self, message, stderr=""):
        Exception.__init__(self, message)
        self.message = message
        self.stderr = stderr


def _git_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line.replace("fatal: ", "")
    return ""


def _git(args, cwd):
    try:
        proc = subprocess.Popen(
            ["git"] + args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            startupinfo=_git_startupinfo(),
        )
        out, err = proc.communicate(timeout=GIT_TIMEOUT)
    except OSError:
        raise GitError("git not found")
    except subprocess.TimeoutExpired:
        proc.kill()
        raise GitError("git timed out")

    if proc.returncode != 0:
        raise GitError(_first_line(err) or "git failed", err)
    return out


def _name_of(path):
    return os.path.basename(path.rstrip("/\\")) or path


def _pretty_path(path):
    # git reports forward slashes on Windows while expanduser yields backslashes;
    # normalise both sides so the prefix matches and the result stays consistent.
    if not path:
        return path
    home = os.path.normcase(os.path.normpath(os.path.expanduser("~"))) + os.sep
    full = os.path.normpath(path)
    if os.path.normcase(full).startswith(home):
        return "~" + os.sep + full[len(home):]
    return full


def _link(url, text, color=None):
    style = ' style="color: {}"'.format(color) if color else ""
    return '<a href="{}"{}>{}</a>'.format(url, style, html.escape(text))


def _annotation(tree, is_current=False):
    # The kind badge would mark the current worktree more prominently, but its
    # container paints a background on every row, including unmarked ones.
    parts = [x for x in (tree.flags, tree.label) if x]
    if is_current:
        parts.append("●")
    return "  ".join(parts)


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b)
    )


class Worktree(object):
    def __init__(self, path, branch="", head="", detached=False, bare=False,
                 locked=False, prunable=False, is_main=False):
        self.path = path
        self.branch = branch
        self.head = head
        self.detached = detached
        self.bare = bare
        self.locked = locked
        self.prunable = prunable
        self.is_main = is_main

    @property
    def name(self):
        return _name_of(self.path)

    @property
    def label(self):
        if self.branch:
            return self.branch
        if self.bare:
            return "(bare)"
        if self.detached:
            return "(detached {})".format(self.head[:7])
        return ""

    @property
    def flags(self):
        return " ".join(
            f for f, on in (("locked", self.locked), ("prunable", self.prunable)) if on
        )


def _parse_worktrees(out):
    # "branch" is absent when detached or bare, so group by blank-line-separated
    # record rather than scanning lines independently.
    trees = []
    record = {}

    def flush():
        path = record.get("worktree")
        if not path:
            return
        branch = record.get("branch", "")
        trees.append(Worktree(
            path=path,
            branch=branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else "",
            head=record.get("HEAD", ""),
            detached="detached" in record,
            bare="bare" in record,
            locked="locked" in record,
            prunable="prunable" in record,
            is_main=not trees,
        ))

    for line in out.splitlines():
        if not line.strip():
            flush()
            record = {}
            continue
        key, _, value = line.partition(" ")
        record[key] = value
    flush()
    return trees


class GitWorktreeSwitchCommand(sublime_plugin.WindowCommand):
    def is_enabled(self):
        return bool(self.window.folders())

    def run(self):
        folders = self.window.folders()
        if not folders:
            return
        self.window.status_message("Listing worktrees...")
        thread = threading.Thread(target=self._collect, args=(folders[0],))
        thread.daemon = True
        thread.start()

    def _status(self, message):
        sublime.set_timeout(lambda: self.window.status_message(message), 0)

    def _collect(self, cwd):
        try:
            out = _git(["worktree", "list", "--porcelain"], cwd)
        except GitError as err:
            self._status(err.message)
            return

        trees = _parse_worktrees(out)
        if not trees:
            self._status("No worktrees found")
            return
        # Hop back to the main thread; UI calls are not thread-safe.
        sublime.set_timeout(lambda: self._show(trees, cwd), 0)

    def _show(self, trees, current):
        items = []
        selected = -1
        for i, tree in enumerate(trees):
            is_current = _same_path(tree.path, current)
            if is_current:
                selected = i
            details = _link(
                sublime.command_url("open_dir", {"dir": tree.path}),
                _pretty_path(tree.path),
            )
            if not _remove_blocker(tree, current):
                details += "  " + _link(
                    sublime.command_url(
                        "git_worktree_remove", {"path": tree.path}
                    ),
                    "[delete]",
                    DANGER,
                )
            items.append(sublime.QuickPanelItem(
                tree.name, details, _annotation(tree, is_current)
            ))
        self.window.show_quick_panel(
            items,
            lambda index, event=None: self._on_done(trees, index, event),
            sublime.WANT_EVENT,
            selected,
        )

    def _on_done(self, trees, index, event=None):
        if index == -1:
            return
        tree = trees[index]
        if (event or {}).get("modifier_keys", {}).get("shift"):
            self.window.run_command("git_worktree_remove", {"path": tree.path})
            return
        if tree.prunable:
            self._status("Worktree is prunable; its directory is gone")
            return
        data = dict(self.window.project_data() or {})
        folders = data.get("folders") or [{}]
        first = dict(folders[0])
        first["path"] = tree.path
        data["folders"] = [first] + list(folders[1:])
        self.window.set_project_data(data)


def _remove_blocker(tree, current):
    if tree.is_main:
        return "Cannot remove the main worktree"
    if current and _same_path(tree.path, current):
        return "Cannot remove the current worktree"
    return None


def _needs_force(stderr):
    return "use --force" in stderr or "locked working tree" in stderr


class GitWorktreeRemoveCommand(sublime_plugin.WindowCommand):
    def is_enabled(self):
        return bool(self.window.folders())

    def run(self, path=None):
        folders = self.window.folders()
        if not folders:
            return
        cwd = folders[0]
        if path:
            # The link fires while the panel is still up; close it so the dialog
            # is not stacked behind an overlay.
            self.window.run_command("hide_overlay")
            self._start(path, cwd)
            return
        self._pick(cwd)

    def _status(self, message):
        sublime.set_timeout(lambda: self.window.status_message(message), 0)

    def _pick(self, cwd):
        def collect():
            try:
                trees = _parse_worktrees(_git(["worktree", "list", "--porcelain"], cwd))
            except GitError as err:
                self._status(err.message)
                return
            removable = [t for t in trees if not _remove_blocker(t, cwd)]
            if not removable:
                self._status("No removable worktrees")
                return
            sublime.set_timeout(lambda: self._show(removable), 0)

        thread = threading.Thread(target=collect)
        thread.daemon = True
        thread.start()

    def _show(self, trees):
        items = []
        for tree in trees:
            details = _link(
                sublime.command_url("open_dir", {"dir": tree.path}),
                _pretty_path(tree.path),
            )
            items.append(sublime.QuickPanelItem(
                tree.name, details, _annotation(tree)
            ))

        def on_done(index):
            if index != -1:
                self._start(trees[index].path, self.window.folders()[0])

        self.window.show_quick_panel(items, on_done, 0, -1, placeholder="Remove worktree")

    def _start(self, path, cwd):
        name = _name_of(path)
        if not sublime.ok_cancel_dialog(
            'Remove worktree "{}"?\n\n{}'.format(name, _pretty_path(path)),
            "Remove",
        ):
            return
        self._run(path, name, cwd, force=False)

    def _run(self, path, name, cwd, force):
        def work():
            args = ["worktree", "remove"]
            if force:
                # git requires -f twice to drop a locked worktree.
                args += ["--force", "--force"]
            args.append(path)
            try:
                _git(args, cwd)
            except GitError as err:
                if not force and _needs_force(err.stderr):
                    sublime.set_timeout(lambda: self._confirm_force(path, name, cwd), 0)
                else:
                    self._status(err.message)
                return
            self._status('Removed worktree "{}"'.format(name))

        thread = threading.Thread(target=work)
        thread.daemon = True
        thread.start()

    def _confirm_force(self, path, name, cwd):
        if sublime.ok_cancel_dialog(
            '"{}" has uncommitted changes, untracked files, or is locked.\n\n'
            "Force removal permanently discards them.".format(name),
            "Force remove",
        ):
            self._run(path, name, cwd, force=True)

