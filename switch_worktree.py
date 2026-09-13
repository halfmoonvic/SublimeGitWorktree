import os
import subprocess
import threading

import sublime
import sublime_plugin


GIT_TIMEOUT = 10


def _git_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b)
    )


def _parse_worktrees(out):
    # "branch" is absent when detached or bare, so group by blank-line-separated
    # record rather than scanning lines independently.
    entries = []
    record = {}

    def flush():
        path = record.get("worktree")
        if not path or "prunable" in record:
            return
        branch = record.get("branch", "")
        if branch.startswith("refs/heads/"):
            label = branch[len("refs/heads/"):]
        elif "bare" in record:
            label = "(bare)"
        elif "detached" in record:
            label = "(detached {})".format(record.get("HEAD", "")[:7])
        else:
            label = ""
        entries.append((path, label))

    for line in out.splitlines():
        if not line.strip():
            flush()
            record = {}
            continue
        key, _, value = line.partition(" ")
        record[key] = value
    flush()
    return entries


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
            out = subprocess.check_output(
                ["git", "worktree", "list", "--porcelain"],
                cwd=cwd,
                universal_newlines=True,
                stderr=subprocess.DEVNULL,
                startupinfo=_git_startupinfo(),
                timeout=GIT_TIMEOUT,
            )
        except OSError:
            self._status("git not found")
            return
        except subprocess.TimeoutExpired:
            self._status("git timed out")
            return
        except subprocess.CalledProcessError:
            self._status("Not a git repository")
            return

        entries = _parse_worktrees(out)
        if not entries:
            self._status("No worktrees found")
            return
        # Hop back to the main thread; UI calls are not thread-safe.
        sublime.set_timeout(lambda: self._show(entries, cwd), 0)

    def _show(self, entries, current):
        items = []
        selected = -1
        for i, (path, label) in enumerate(entries):
            name = os.path.basename(path.rstrip("/\\")) or path
            if _same_path(path, current):
                selected = i
                name = "> " + name
            items.append([" ".join(x for x in (name, label) if x), path])
        self.window.show_quick_panel(
            items, lambda index: self._on_done(entries, index), 0, selected
        )

    def _on_done(self, entries, index):
        if index == -1:
            return
        data = dict(self.window.project_data() or {})
        folders = data.get("folders") or [{}]
        first = dict(folders[0])
        first["path"] = entries[index][0]
        data["folders"] = [first] + list(folders[1:])
        self.window.set_project_data(data)
