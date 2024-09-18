import logging
import os
import tempfile
from pathlib import Path
from zipfile import ZipFile

from runloop_api_client import APIStatusError, Runloop

from openhands.core.config import AppConfig
from openhands.events import EventStream
from openhands.events.action import (
    BrowseInteractiveAction,
    BrowseURLAction,
    CmdRunAction,
    FileReadAction,
    FileWriteAction,
    IPythonRunCellAction,
)
from openhands.events.observation import (
    CmdOutputObservation,
    ErrorObservation,
    Observation,
)
from openhands.events.observation.files import FileReadObservation, FileWriteObservation
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.runtime import Runtime
from openhands.runtime.utils.files import read_lines


class RunloopRuntime(Runtime):
    """
    The runtime interface for connecting to the Runloop provided cloud Runtime.
    """

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
    ):
        super().__init__(config, event_stream, sid, plugins, env_vars)
        self.api_client = Runloop(
            bearer_token=config.runloop_api_key,
        )
        self.devbox = self.api_client.devboxes.create()

    def run_ipython(self, action: IPythonRunCellAction) -> Observation:
        raise NotImplementedError

    def read(self, action: FileReadAction) -> Observation:
        file_contents = self.api_client.devboxes.read_file_contents(
            id=self.devbox.id, file_path=action.path
        )
        return FileReadObservation(
            content=''.join(
                read_lines(file_contents.split('\n'), action.start, action.end)
            ),
            path=action.path,
        )

    def write(self, action: FileWriteAction) -> Observation:
        contents: str = action.content
        try:
            self.api_client.devboxes.write_file(
                id=self.devbox.id, file_path=action.path, contents=contents
            )

            return FileWriteObservation(
                content='',
                path=action.path,
            )

        except APIStatusError as e:
            return ErrorObservation(
                content=e.message,
            )

        except Exception as e:
            return ErrorObservation(
                content=str(e),
            )

    def browse(self, action: BrowseURLAction) -> Observation:
        raise NotImplementedError

    def browse_interactive(self, action: BrowseInteractiveAction) -> Observation:
        raise NotImplementedError

    def copy_to(self, host_src: str, sandbox_dest: str, recursive: bool = False):
        if recursive:
            # For recursive copy, create a zip file
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
                temp_zip_path = temp_zip.name

                with ZipFile(temp_zip_path, 'w') as zipf:
                    for root, _, files in os.walk(host_src):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(
                                file_path, os.path.dirname(host_src)
                            )
                            zipf.write(file_path, arcname)

                # Upload zip
                self.api_client.devboxes.upload_file(
                    id=self.devbox.id,
                    file=Path(temp_zip.name),
                    # TODO: unique destination for multiple concurrent
                    path='/tmp/uploaded.zip',
                )

                self.api_client.devboxes.execute_sync(
                    command=f'unzip /tmp/uploaded.zip -d {sandbox_dest} && rm /tmp/uploaded.zip'
                )

                # TODO Handle result

        else:
            self.api_client.devboxes.upload_file(
                id=self.devbox.id,
                file=Path(host_src),
                # TODO: unique destination for multiple concurrent
                path=sandbox_dest,
            )

    def list_files(self, path: str | None = None) -> list[str]:
        try:
            result = self.api_client.devboxes.execute_sync(
                id=self.devbox.id, command=f'ls {path}'
            )
            return result.stdout.split('\n')
        except APIStatusError as e:
            logging.error(f'Error listing files: {e}')
            raise e
        except Exception as e:
            logging.error(f'Error listing files: {e}')
            raise e

    def run(self, action: CmdRunAction) -> Observation:
        # TODO: make this async vs sync
        # DO we kill? where do we manage timeout. etc
        try:
            result = self.api_client.devboxes.execute_sync(command=action.command)
            return CmdOutputObservation(
                content=result.stdout,
                command_id=action.id,
                command=action.command,
                exit_code=result.exit_status,
            )

        except APIStatusError as e:
            return ErrorObservation(
                content=e.message,
            )
