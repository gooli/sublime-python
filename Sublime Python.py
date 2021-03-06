import os
import threading
import subprocess
from collections import defaultdict, namedtuple

import sublime
import sublime_plugin

SETTINGS = sublime.load_settings(__name__ + '.sublime-settings')
PYTHON = SETTINGS.get('python_binary', 'python')
APPDIR = os.path.abspath(os.path.split(__file__)[0])

def error(message):
    sublime.message_dialog("Sublime Python\n\n" + message)

SymbolBase = namedtuple('Symbol', 'name type filename line')
class Symbol(SymbolBase):
    @property
    def location(self):
        return '%s:%d' % (self.filename, self.line)

    @property
    def full(self):
        return '%s:%s:%d' % (self.name, self.filename, self.line)

class Symbols(object):
    def __init__(self):
        self._symbols = []
        self._lock = threading.RLock()

    def get_all(self):
        recent_symbols = SETTINGS.get('recent_symbols', [])
        def priority(symbol):
            try:
                return recent_symbols.index(symbol.full)
            except ValueError:
                return (symbol.name, symbol.filename, symbol.line)

        with self._lock:
            symbols = self._symbols[:]
            symbols.sort(key=priority)
            return symbols

    def set_all(self, symbols):
        with self._lock:
            self._symbols = list(set(symbols))

    def set_file_symbols(self, filename, symbols):
        with self._lock:
            self.remove_file_symbols(filename)
            self._symbols.extend(symbols)

    def remove_file_symbols(self, filename):
        with self._lock:
            self._symbols = [sym for sym in self._symbols if sym.filename != filename]

class SymbolManager(object):
    THREAD_NAME = "__python_symbols_thread__"

    def __init__(self):
        self._symbols = Symbols()
        self._thread = None
        self._progress = None
        self.loaded = False

    def get_symbols(self):
        return self._symbols.get_all()

    def scan_all(self):
        options = []
        for directory in sublime.active_window().folders():
            options.extend(['-d', directory])
        for directory in SETTINGS.get('include_dirs', []):
            options.extend(['-d', directory])
        for view in sublime.active_window().views():
            if view.file_name():
                options.extend(['-f', view.file_name()])
        def callback(symbols):
            self._symbols.set_all(symbols)
            self.loaded = True
        self._scan(options, callback)

    def scan_file(self, filename):
        def callback(symbols):
            self._symbols.set_file_symbols(filename, symbols)
        self._scan(['-f', filename], callback)

    def remove_file(self, filename):
        self._symbols.remove_file_symbols(filename)

    def _scan(self, options, callback):
        old_threads = [t for t in threading.enumerate() if t.name == self.THREAD_NAME]
        if old_threads:
            sublime.status_message("already scanning, please wait")
            return
        self._progress = 0
        self._show_progress()
        self._thread = threading.Thread(
            target=lambda: self._scan_thread(options, callback),
            name=self.THREAD_NAME
        )
        self._thread.daemon = True
        self._thread.start()

    def _scan_thread(self, options, callback):
        symbols = []
        def add_symbol(name, type, filename, line):
            symbols.append(Symbol(name, type, filename, line))
        exclude = []
        for ex in SETTINGS.get('exclude_dirs', []):
            exclude.extend(['-x', ex])
        process = subprocess.Popen([PYTHON, '-u', '%s/symbols.py' % APPDIR] + exclude + options,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            # using readline istead of 'for line in stdout' to avoid buffering
            line = process.stdout.readline()
            if not line:
                break
            eval(line, dict(
                progress=self._update_progress,
                symbol=add_symbol
            ))
        # error = process.stderr.read()
        # if error:
        #     print "Sublime Python detected errrors while scanning files:"
        #     print error
        self._update_progress(None)
        callback(symbols)

    def _update_progress(self, percent):
        self._progress = percent

    def _show_progress(self):
        if self._progress is not None:
            sublime.status_message("scanning symbols (%d%% done)..." % self._progress)
            sublime.set_timeout(self._show_progress, 200)
        else:
            sublime.status_message("")

def add_recent_symbol(symbol):
    settings = sublime.load_settings(__name__ + '.sublime-settings')
    recent_symbols = settings.get('recent_symbols', [])
    recent_symbols = [s for s in recent_symbols if s != symbol.full]
    recent_symbols = recent_symbols[:20] # keep list short
    recent_symbols.insert(0, symbol.full)
    settings.set('recent_symbols', recent_symbols)
    sublime.save_settings(__name__ + '.sublime-settings')

def goto_symbol(window, symbols):
    def on_selection(index):
        if index == -1:
            return
        add_recent_symbol(symbols[index])
        window.open_file(symbols[index].location+':0', sublime.ENCODED_POSITION)
    if not symbols:
        error("No matching symbols found.")
    elif len(symbols) == 1:
        on_selection(0)
    else:
        menu_items = [[sym.name, sym.location] for sym in symbols]
        window.show_quick_panel(menu_items, on_selection)

class SublimePythonGotoDialogCommand(sublime_plugin.WindowCommand):
    def run(self):
        manager = MANAGERS[self.window.id()]
        if not manager.loaded:
            error("Loading symbols, please try in a few moments...")
            manager.scan_all()
            return
        goto_symbol(self.window, manager.get_symbols())

class SublimePythonGotoWordCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        manager = MANAGERS[self.view.window().id()]
        if not manager.loaded:
            error("Loading symbols, please try in a few moments...")
            manager.scan_all()
            return
        word = self.view.substr(self.view.word(self.view.sel()[0]))
        if not word:
            return
        symbols = [sym for sym in manager.get_symbols() if word == sym.name]
        goto_symbol(self.view.window(), symbols)

class SublimePythonScanCommand(sublime_plugin.WindowCommand):
    def run(self):
        MANAGERS[sublime.active_window().id()].scan_all()

class SublimePythonEventListener(sublime_plugin.EventListener):
    def __init__(self):
        super(SublimePythonEventListener, self).__init__()
        self.prev_folders = {}

    def on_load(self, view):
        window_id = view.window().id()
        if window_id not in self.prev_folders:
            self.prev_folders[window_id] = sublime.active_window().folders()
        manager = MANAGERS[window_id]
        if not manager.loaded or self.prev_folders[window_id] != sublime.active_window().folders():
            self.prev_folders[window_id] = sublime.active_window().folders()
            manager.scan_all()
        else:
            manager.scan_file(view.file_name())

    def on_close(self, view):
        manager = MANAGERS[sublime.active_window().id()]
        file_name = view.file_name()
        for folder in sublime.active_window().folders():
            if file_name and file_name.startswith(folder):
                break
        else:
            manager.remove_file(file_name)

    def on_post_save(self, view):
        manager = MANAGERS[view.window().id()]
        manager.scan_file(view.file_name())

MANAGERS = defaultdict(SymbolManager)
