"""Run the native stream with optional plugins and shared startup services."""

import asyncio
import sys

from ish.lang import Message, i18n
from ish.log import session_logging

from .startup import command, parse, report_logs


def main(argv=None):
    """Resolve read-only options before creating logs, plugins or a shell."""
    try:
        parser, options, finished = parse(argv)
        if finished:
            return 0
        shell = command(parser, options)
        with session_logging(level=options.log_level, path=options.log_file) as log:
            report_logs(options.log_records, log.logger)
            try:
                from .app import App

                app = App(
                    shell,
                    str(options.cwd),
                    integration_dir=options.integration_dir,
                    plugins=options.plugin,
                    plugin_dir=options.plugin_dir,
                    library_dir=options.plugin_lib_dir,
                    logger=log.logger,
                    report=log.flush,
                )
                result = asyncio.run(app.run())
                if app.error:
                    log.logger.error(
                        i18n.get(Message.STREAM_SESSION_ERROR, error=app.error)
                    )
                    return 1
                return result if result >= 0 else 128 - result
            except (OSError, RuntimeError, ValueError) as error:
                log.logger.error(i18n.get(Message.STREAM_SESSION_ERROR, error=error))
                return 1
    except (OSError, RuntimeError, ValueError) as error:
        print(i18n.get(Message.STREAM_SESSION_ERROR, error=error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
