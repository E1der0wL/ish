"""Run the separate hookless VT entry point with shared startup services."""

import asyncio
import signal
import sys

from ish.lang import Message, i18n
from ish.log import session_logging

from .startup import command, parse, report_logs

STOP_SIGNALS = ("SIGTERM", "SIGHUP")


async def run_terminal(app):
    """Convert CLI termination signals into the application's normal cleanup."""
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        for name in STOP_SIGNALS:
            signum = getattr(signal, name)
            previous[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, app.stop, signum)
        previous[signal.SIGTSTP] = signal.getsignal(signal.SIGTSTP)
        loop.add_signal_handler(signal.SIGTSTP, app.suspend)
        return await app.run()
    finally:
        for signum, handler in previous.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)


def main(argv=None):
    """Select a native interactive shell and start the PTK terminal application."""
    try:
        parser, args, finished = parse(argv)
        if finished:
            return 0
        shell = command(parser, args)
        # PluginManager shares the normal logger. Its diagnostics must not write
        # into the PTK screen while a selected plugin runs or is being cleaned up.
        with session_logging(level=args.log_level, path=args.log_file) as log:
            report_logs(args.log_records, log.logger)
            app = None
            try:
                from .app import TerminalApp

                log.logger.info(
                    i18n.get(
                        Message.VT_SESSION_START,
                        shell=shell[0],
                        plugins=len(args.plugin),
                    )
                )
                app = TerminalApp(
                    shell[0],
                    args.cwd,
                    shell_args=shell[1:],
                    dump_state=args.dump_state,
                    plugins=args.plugin,
                    plugin_dir=args.plugin_dir,
                    library_dir=args.plugin_lib_dir,
                    clipboard=args.clipboard,
                    mouse=args.mouse,
                    color_depth=args.color_depth,
                    features=args.features,
                    escape_delay=args.escape_delay,
                    scrollback_lines=args.scrollback_lines,
                    scrollback_bytes=args.scrollback_bytes,
                )
                result = asyncio.run(run_terminal(app))
                log.logger.info(i18n.get(Message.VT_SESSION_EXIT, status=result))
            except (OSError, RuntimeError, TypeError, ValueError, BufferError) as exc:
                log.logger.error(i18n.get(Message.VT_SESSION_ERROR, error=exc))
                return 1
            finally:
                # Native records are counts only. Report after PTK restores the
                # host, still inside the deferred shared logging lifetime.
                from .diagnostics import report, report_session

                if app is not None:
                    report_session(app, log.logger)
                report()
    except (OSError, RuntimeError, TypeError, ValueError, BufferError) as exc:
        print(i18n.get(Message.VT_SESSION_ERROR, error=exc), file=sys.stderr)
        return 1
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
