import os
import subprocess
import threading

import sublime
import sublime_plugin


GIT_TIMEOUT = 10


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
        return os.path.basename(self.path.rstrip("/\\")) or self.path

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


class SwitchWorktreeCommand(sublime_plugin.WindowCommand):
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
            name = tree.name
            if _same_path(tree.path, current):
                selected = i
                name = "> " + name
            trigger = " ".join(x for x in (name, tree.label) if x)
            items.append([trigger, tree.path])
        self.window.show_quick_panel(
            items, lambda index: self._on_done(trees, index), 0, selected
        )

    def _on_done(self, trees, index):
        if index == -1:
            return
        tree = trees[index]
        if tree.prunable:
            self._status("Worktree is prunable; its directory is gone")
            return
        data = dict(self.window.project_data() or {})
        folders = data.get("folders") or [{}]
        first = dict(folders[0])
        first["path"] = tree.path
        data["folders"] = [first] + list(folders[1:])
        self.window.set_project_data(data)
