from .configurations import ConfigManager
from .configurations import WindowConfigManager
from .diagnostics import DiagnosticsStorage
from .logging import debug
from .logging import exception_log
from .message_request_handler import MessageRequestHandler
from .protocol import Error
from .protocol import Notification
from .rpc import Logger
from .sessions import get_plugin
from .sessions import Manager
from .sessions import Session
from .settings import settings
from .transports import create_transport
from .types import ClientConfig
from .types import Settings
from .types import view2scope
from .types import ViewLike
from .types import WindowLike
from .typing import Optional, Callable, Any, Deque, Protocol, Generator
from .views import extract_variables
from .workspace import disable_in_project
from .workspace import enable_in_project
from .workspace import ProjectFolders
from .workspace import sorted_workspace_folders
from abc import ABCMeta
from abc import abstractmethod
from collections import deque
from weakref import ref
from weakref import WeakSet
from weakref import WeakValueDictionary
import sublime


class SublimeLike(Protocol):

    def set_timeout_async(self, f: Callable, timeout_ms: int = 0) -> None:
        ...

    def Region(self, a: int, b: int) -> 'Any':
        ...


class AbstractViewListener(metaclass=ABCMeta):

    view = None  # type: sublime.View

    @abstractmethod
    def on_session_initialized(self, session: Session) -> None:
        raise NotImplementedError()

    @abstractmethod
    def on_session_shutdown(self, session: Session) -> None:
        raise NotImplementedError()

    @abstractmethod
    def __hash__(self) -> int:
        raise NotImplementedError()


def extract_message(params: Any) -> str:
    return params.get("message", "???") if isinstance(params, dict) else "???"


class WindowManager(Manager):
    def __init__(
        self,
        window: WindowLike,
        workspace: ProjectFolders,
        settings: Settings,
        configs: WindowConfigManager,
        diagnostics: DiagnosticsStorage,
        sublime: Any,
        server_panel_factory: Optional[Callable] = None
    ) -> None:
        self._window = window
        self._settings = settings
        self._configs = configs
        self.diagnostics = diagnostics
        self.server_panel_factory = server_panel_factory
        self._sessions = WeakSet()  # type: WeakSet[Session]
        self._sublime = sublime
        self._restarting = False
        self._is_closing = False
        self._workspace = workspace
        self._pending_listeners = deque()  # type: Deque[AbstractViewListener]
        self._listeners = WeakSet()  # type: WeakSet[AbstractViewListener]
        self._new_listener = None  # type: Optional[AbstractViewListener]
        self._new_session = None  # type: Optional[Session]

    def on_load_project(self) -> None:
        # TODO: Also end sessions that were previously enabled in the .sublime-project, but now disabled or removed
        # from the .sublime-project.
        self._configs.update()
        workspace_folders = self._workspace.update()
        for session in self._sessions:
            session.update_folders(workspace_folders)

    def on_pre_close_project(self) -> None:
        self.end_sessions()

    def register_listener(self, listener: AbstractViewListener) -> None:
        self._pending_listeners.appendleft(listener)
        if self._new_session:
            return
        sublime.set_timeout_async(self._dequeue_listener)

    def unregister_listener(self, listener: AbstractViewListener) -> None:
        self._listeners.discard(listener)

    def listeners(self) -> Generator[AbstractViewListener, None, None]:
        yield from self._listeners

    def _dequeue_listener(self) -> None:
        listener = None  # type: Optional[AbstractViewListener]
        if self._new_listener is not None and self._new_listener.view.is_valid():
            listener = self._new_listener
            self._new_listener = None
        else:
            try:
                listener = self._pending_listeners.pop()
                if not listener.view.is_valid():
                    sublime.set_timeout_async(self._dequeue_listener)
                    return
                self._listeners.add(listener)
            except IndexError:
                # We have handled all pending listeners.
                self._new_session = None
                return
        if self._new_session:
            self._sessions.add(self._new_session)
        self._publish_sessions_to_listener(listener)
        self._new_session = None
        config = self._needed_config(listener.view)
        if config:
            self._new_listener = listener
            self.start(config, listener.view)
        else:
            self._new_listener = None

    def _publish_sessions_to_listener(self, listener: AbstractViewListener) -> None:
        try:
            scope = view2scope(listener.view)
        except IndexError:
            return
        if listener.view in self._workspace:
            for session in self._sessions:
                if session.can_handle(listener.view):
                    listener.on_session_initialized(session)
        else:
            for session in self._sessions:
                if session.config.match_document(scope):
                    listener.on_session_initialized(session)
                    break

    def window(self) -> sublime.Window:
        # WindowLike vs. sublime
        return self._window  # type: ignore

    def sessions(self, view: sublime.View, capability: Optional[str] = None) -> Generator[Session, None, None]:
        for session in self._sessions:
            if session.can_handle(view, capability):
                yield session

    def get_session(self, config_name: str, file_path: str) -> Optional[Session]:
        return self._find_session(config_name, file_path)

    def _can_start_config(self, config_name: str, file_path: str) -> bool:
        return not bool(self._find_session(config_name, file_path))

    def _find_session(self, config_name: str, file_path: str) -> Optional[Session]:
        for session in self._sessions:
            if session.config.name == config_name and session.handles_path(file_path):
                return session
        return None

    def enable_config(self, config_name: str) -> None:
        enable_in_project(self._window, config_name)
        self._configs.update()
        for listener in self._listeners:
            self._pending_listeners.appendleft(listener)
        self._listeners.clear()
        if not self._new_session:
            sublime.set_timeout_async(self._dequeue_listener)

    def disable_config(self, config_name: str) -> None:
        disable_in_project(self._window, config_name)
        self._configs.update()
        self.end_config_sessions(config_name)

    def _needed_config(self, view: sublime.View) -> Optional[ClientConfig]:
        configs = self._configs.match_view(view)
        handled = False
        file_name = view.file_name() or ''
        if view in self._workspace:
            for config in configs:
                handled = False
                for session in self._sessions:
                    if config.name == session.config.name and session.handles_path(file_name):
                        handled = True
                        break
                if not handled:
                    return config
        else:
            for config in configs:
                handled = False
                for session in self._sessions:
                    if config.name == session.config.name:
                        handled = True
                        break
                if not handled:
                    return config
        return None

    def start(self, config: ClientConfig, initiating_view: sublime.View) -> None:
        file_path = initiating_view.file_name() or ''
        if not self._can_start_config(config.name, file_path):
            debug('Already starting on this window:', config.name)
            return
        try:
            workspace_folders = sorted_workspace_folders(self._workspace.folders, file_path)
            plugin_class = get_plugin(config.name)
            if plugin_class is not None:
                if plugin_class.needs_update_or_installation():
                    self._window.status_message('Installing {} ...'.format(config.name))
                    plugin_class.install_or_update()
                # WindowLike vs. sublime.Window
                cannot_start_reason = plugin_class.can_start(
                    self._window, initiating_view, workspace_folders, config)  # type: ignore
                if cannot_start_reason:
                    self._window.status_message(cannot_start_reason)
                    return
            session = Session(self, PanelLogger(self, config.name), workspace_folders, config, plugin_class)
            cwd = workspace_folders[0].path if workspace_folders else None
            variables = extract_variables(self._window)  # type: ignore
            if plugin_class is not None:
                additional_variables = plugin_class.additional_variables()
                if isinstance(additional_variables, dict):
                    variables.update(additional_variables)
            # WindowLike vs sublime.Window
            self._window.status_message("Starting {} ...".format(config.name))
            transport = create_transport(config, cwd, self._window, session, variables)  # type: ignore
            self._window.status_message("Initializing {} ...".format(config.name))
            session.initialize(variables, transport)
            debug("window {} added session {}".format(self._window.id(), config.name))
            self._new_session = session
        except Exception as e:
            message = "\n\n".join([
                "Could not start {}",
                "{}",
                "Server will be disabled for this window"
            ]).format(config.name, str(e))
            exception_log("Unable to start {}".format(config.name), e)
            self._configs.disable_temporarily(config.name)
            sublime.message_dialog(message)
            # Continue with handling pending listeners
            self._new_session = None
            sublime.set_timeout_async(self._dequeue_listener)

    def handle_message_request(self, session: Session, params: Any, request_id: Any) -> None:
        handler = MessageRequestHandler(self._window.active_view(), session, request_id, params,  # type: ignore
                                        session.config.name)
        handler.show()

    def restart_sessions(self) -> None:
        self._restarting = True
        self.end_sessions()

    def end_sessions(self) -> None:
        sessions = list(self._sessions)
        for session in sessions:
            session.end()

    def end_config_sessions(self, config_name: str) -> None:
        sessions = list(self._sessions)
        for session in sessions:
            if session.config.name == config_name:
                session.end()

    def get_project_path(self, file_path: str) -> Optional[str]:
        candidate = None  # type: Optional[str]
        for folder in self._workspace.folders:
            if file_path.startswith(folder):
                if candidate is None or len(folder) > len(candidate):
                    candidate = folder
        return candidate

    def on_post_initialize(self, session: Session) -> None:
        session.send_notification(Notification.initialized())
        self._window.status_message("{} initialized".format(session.config.name))
        sublime.set_timeout_async(self._dequeue_listener)

    def handle_view_closed(self, view: ViewLike) -> None:
        if view.file_name():
            if not self._is_closing:
                if not self._window.is_valid():
                    # try to detect close synchronously (for quitting)
                    self._handle_window_closed()
                else:
                    # in case the window is invalidated after the last view is closed
                    self._sublime.set_timeout_async(lambda: self._check_window_closed(), 100)

    def _check_window_closed(self) -> None:
        if not self._is_closing and not self._window.is_valid():
            self._handle_window_closed()

    def _handle_window_closed(self) -> None:
        debug('window {} closed, ending sessions'.format(self._window.id()))
        self._is_closing = True
        self.end_sessions()

    def _handle_all_sessions_ended(self) -> None:
        debug('clients for window {} unloaded'.format(self._window.id()))
        if self._restarting:
            debug('window {} sessions unloaded - restarting'.format(self._window.id()))

    def on_post_exit(self, session: Session, exit_code: int, exception: Optional[Exception]) -> None:
        sublime.set_timeout(lambda: self._on_post_exit_main_thread(session, exit_code, exception))

    def _on_post_exit_main_thread(self, session: Session, exit_code: int, exception: Optional[Exception]) -> None:
        for view in self._window.views():
            file_name = view.file_name()
            if file_name:
                self.diagnostics.remove(file_name, session.config.name)
        for listener in self._listeners:
            listener.on_session_shutdown(session)
        if exit_code != 0:
            self._window.status_message("{} exited with status code {}".format(session.config.name, exit_code))
            fmt = "{0} exited with status code {1}.\n\nDo you want to restart {0}?\n\nIf you choose Cancel, {0} will "\
                  "be disabled for this window until you restart Sublime Text."
            msg = fmt.format(session.config.name, exit_code)
            if sublime.ok_cancel_dialog(msg, "Restart {}".format(session.config.name)):
                v = self._window.active_view()
                if not v:
                    return
                sublime.set_timeout(lambda: self.start(session.config, v))  # type: ignore
            else:
                self._configs.disable_temporarily(session.config.name)
        if exception:
            self._window.status_message("{} exited with an exception: {}".format(session.config.name, exception))
        self._sessions.discard(session)  # TODO: This should be automatic!
        if not self._sessions:
            self._handle_all_sessions_ended()

    def handle_server_message(self, server_name: str, message: str) -> None:
        if not self.server_panel_factory:
            return
        panel = self.server_panel_factory(self._window)
        if not panel:
            return debug("no server panel for window", self._window.id())
        panel.run_command("lsp_update_server_panel", {"prefix": server_name, "message": message})

    def handle_log_message(self, session: Session, params: Any) -> None:
        self.handle_server_message(session.config.name, extract_message(params))

    def handle_stderr_log(self, session: Session, message: str) -> None:
        if self._settings.log_stderr:
            self.handle_server_message(session.config.name, message)

    def handle_show_message(self, session: Session, params: Any) -> None:
        self._sublime.status_message("{}: {}".format(session.config.name, extract_message(params)))


class WindowRegistry(object):
    def __init__(self, configs: ConfigManager, sublime: Any) -> None:
        self._windows = WeakValueDictionary()  # type: WeakValueDictionary[int, WindowManager]
        self._configs = configs
        self._sublime = sublime
        self._diagnostics_ui_class = None  # type: Optional[Callable]
        self._server_panel_factory = None  # type: Optional[Callable]
        self._settings = None  # type: Optional[Settings]

    def set_diagnostics_ui(self, ui_class: Any) -> None:
        self._diagnostics_ui_class = ui_class

    def set_server_panel_factory(self, factory: Callable) -> None:
        self._server_panel_factory = factory

    def set_settings_factory(self, settings: Settings) -> None:
        self._settings = settings

    def lookup(self, window: Any) -> WindowManager:
        state = self._windows.get(window.id())
        if state is None:
            if not self._settings:
                raise RuntimeError("no settings")
            workspace = ProjectFolders(window)
            window_configs = self._configs.for_window(window)
            diagnostics_ui = self._diagnostics_ui_class(window) if self._diagnostics_ui_class else None
            state = WindowManager(
                window=window,
                workspace=workspace,
                settings=self._settings,
                configs=window_configs,
                diagnostics=DiagnosticsStorage(diagnostics_ui),
                sublime=self._sublime,
                server_panel_factory=self._server_panel_factory)
            self._windows[window.id()] = state
        return state


class PanelLogger(Logger):

    def __init__(self, manager: WindowManager, server_name: str) -> None:
        self._manager = ref(manager)
        self._server_name = server_name

    def log(self, message: str, params: Any, log_payload: bool) -> None:

        def run_on_async_worker_thread() -> None:
            nonlocal message
            if log_payload:
                message = "{}: {}".format(message, params)
            manager = self._manager()
            if manager is not None:
                manager.handle_server_message(":", message)

        sublime.set_timeout_async(run_on_async_worker_thread)

    def outgoing_response(self, request_id: Any, params: Any) -> None:
        if not settings.log_debug:
            return
        self.log(self._format_response(">>>", request_id), params, settings.log_payloads)

    def outgoing_error_response(self, request_id: Any, error: Error) -> None:
        if not settings.log_debug:
            return
        self.log(self._format_response("~~>", request_id), error.to_lsp(), settings.log_payloads)

    def outgoing_request(self, request_id: int, method: str, params: Any, blocking: bool) -> None:
        if not settings.log_debug:
            return
        direction = "==>" if blocking else "-->"
        self.log(self._format_request(direction, method, request_id), params, settings.log_payloads)

    def outgoing_notification(self, method: str, params: Any) -> None:
        if not settings.log_debug:
            return
        # Do not log the payloads if any of these conditions occur because the payloads might contain the entire
        # content of the view.
        log_payload = settings.log_payloads
        if method.endswith("didOpen"):
            log_payload = False
        elif method.endswith("didChange"):
            content_changes = params.get("contentChanges")
            if content_changes and "range" not in content_changes[0]:
                log_payload = False
        elif method.endswith("didSave"):
            if isinstance(params, dict) and "text" in params:
                log_payload = False
        self.log(self._format_notification(" ->", method), params, log_payload)

    def incoming_response(self, request_id: int, params: Any, is_error: bool, blocking: bool) -> None:
        if not settings.log_debug:
            return
        if is_error:
            direction = "<~~"
        else:
            direction = "<==" if blocking else "<<<"
        self.log(self._format_response(direction, request_id), params, settings.log_payloads)

    def incoming_error_response(self, request_id: Any, error: Any) -> None:
        if not settings.log_debug:
            return
        self.log(self._format_response('<~~', request_id), error, settings.log_payloads)

    def incoming_request(self, request_id: Any, method: str, params: Any) -> None:
        if not settings.log_debug:
            return
        self.log(self._format_request("<--", method, request_id), params, settings.log_payloads)

    def incoming_notification(self, method: str, params: Any, unhandled: bool) -> None:
        if not settings.log_debug or method == "window/logMessage":
            return
        direction = "<? " if unhandled else "<- "
        self.log(self._format_notification(direction, method), params, settings.log_payloads)

    def _format_response(self, direction: str, request_id: Any) -> str:
        return "{} {} {}".format(direction, self._server_name, request_id)

    def _format_request(self, direction: str, method: str, request_id: Any) -> str:
        return "{} {} {}({})".format(direction, self._server_name, method, request_id)

    def _format_notification(self, direction: str, method: str) -> str:
        return "{} {} {}".format(direction, self._server_name, method)
