import html
import os
import re
import subprocess
import threading

import sublime
import sublime_plugin


GIT_TIMEOUT = 10

# Resolved against the active color scheme, so it tracks the user's theme.
DANGER = "var(--redish)"

SETTINGS = "git_worktree.sublime-settings"
DEFAULT_PATH = "../{name}"

BAD_CHARS = re.compile('[\x00-\x1f<>:"|?*\\\\]')
RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


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
    # git writes progress to stderr too ("Preparing worktree..."), so the first
    # line is often not the error; prefer the fatal one when it is there.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if line.startswith("fatal: "):
            return line[len("fatal: "):]
    return lines[0] if lines else ""


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


def _dir_name(name):
    # A branch may contain "/", but using it in the directory name would nest the
    # worktree a level deeper, so the two names deliberately differ.
    return name.replace("/", "-")


def _name_error(name):
    if not name or not name.strip():
        return "Name cannot be empty"
    if name != name.strip():
        return "Name cannot have leading or trailing spaces"
    directory = _dir_name(name)
    if BAD_CHARS.search(directory):
        return "Name contains an invalid character"
    if directory in (".", ".."):
        return "Invalid name"
    if directory.lower() in RESERVED:
        return 'Name "{}" is reserved on Windows'.format(directory)
    return None


def _target_path(name, main_path, template):
    project = _name_of(main_path)
    try:
        path = template.format(name=_dir_name(name), project=project)
    except (KeyError, IndexError):
        # An unknown placeholder in user settings would otherwise raise here.
        path = DEFAULT_PATH.format(name=_dir_name(name), project=project)
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(main_path, path)
    return os.path.normpath(path)


def _occupied(path):
    return os.path.isdir(path) and bool(os.listdir(path))


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


def _switch_to(window, path):
    # Only folders[0]["path"] may change: the rest of the dict carries "name",
    # "folder_exclude_patterns" and friends, and set_project_data writes to disk
    # immediately with no undo.
    data = dict(window.project_data() or {})
    folders = data.get("folders") or [{}]
    first = dict(folders[0])
    first["path"] = path
    data["folders"] = [first] + list(folders[1:])
    window.set_project_data(data)


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


def _main_worktree_path(cwd):
    # The first porcelain record is the main worktree even when git runs from a
    # linked one, which makes it a stable anchor for relative path templates.
    trees = _parse_worktrees(_git(["worktree", "list", "--porcelain"], cwd))
    return trees[0].path if trees else cwd


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
        _switch_to(self.window, tree.path)


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


def _current_branch(cwd):
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    return "HEAD" if branch == "HEAD" else branch


class GitWorktreeAddCommand(sublime_plugin.WindowCommand):
    def is_enabled(self):
        return bool(self.window.folders())

    def run(self, name=None, base=None):
        folders = self.window.folders()
        if not folders:
            return
        cwd = folders[0]
        if name:
            self._start(name, base, cwd)
            return
        # The panel is still up when this runs from a link in a quick panel row.
        self.window.run_command("hide_overlay")
        self._prompt(base, cwd)

    def _status(self, message):
        sublime.set_timeout(lambda: self.window.status_message(message), 0)

    def _template(self):
        template = sublime.load_settings(SETTINGS).get("worktree_path", DEFAULT_PATH)
        return template if isinstance(template, str) else DEFAULT_PATH

    def _prompt(self, base, cwd):
        def collect():
            try:
                main_path = _main_worktree_path(cwd)
            except GitError as err:
                self._status(err.message)
                return
            sublime.set_timeout(lambda: show(main_path), 0)

        def show(main_path):
            template = self._template()

            def on_change(text):
                text = text.strip()
                if not text:
                    self.window.status_message("")
                    return
                error = _name_error(text)
                if error:
                    self.window.status_message(error)
                else:
                    # Show where it will land before the user commits to it.
                    self.window.status_message(
                        _pretty_path(_target_path(text, main_path, template))
                    )

            def on_done(text):
                self.window.status_message("")
                self._start(text.strip(), base, cwd)

            self.window.show_input_panel(
                "New worktree name:", "", on_done, on_change, None
            )

        thread = threading.Thread(target=collect)
        thread.daemon = True
        thread.start()

    def _start(self, name, base, cwd):
        error = _name_error(name)
        if error:
            self._status(error)
            return

        def work():
            try:
                _git(["check-ref-format", "--branch", name], cwd)
            except GitError:
                self._status('"{}" is not a valid branch name'.format(name))
                return
            try:
                main_path = _main_worktree_path(cwd)
                ref = base or _current_branch(cwd)
            except GitError as err:
                self._status(err.message)
                return

            path = _target_path(name, main_path, self._template())
            if _occupied(path):
                self._status("{} already exists".format(_pretty_path(path)))
                return
            self._add(name, path, ref, cwd, new_branch=True)

        thread = threading.Thread(target=work)
        thread.daemon = True
        thread.start()

    def _add(self, name, path, ref, cwd, new_branch):
        if new_branch:
            args = ["worktree", "add", "-b", name, path, ref]
        else:
            # Checking out an existing branch: it names itself, there is no base.
            args = ["worktree", "add", path, name]
        try:
            _git(args, cwd)
        except GitError as err:
            if new_branch and "already exists" in err.stderr:
                sublime.set_timeout(
                    lambda: self._confirm_checkout(name, path, ref, cwd), 0
                )
            else:
                self._status(err.message)
            return
        self._status('Created worktree "{}"'.format(name))
        sublime.set_timeout(lambda: _switch_to(self.window, path), 0)

    def _confirm_checkout(self, name, path, ref, cwd):
        if not sublime.ok_cancel_dialog(
            'Branch "{}" already exists.\n\n'
            "Create the worktree from that existing branch instead?".format(name),
            "Use existing",
        ):
            return

        def work():
            self._add(name, path, ref, cwd, new_branch=False)

        thread = threading.Thread(target=work)
        thread.daemon = True
        thread.start()

