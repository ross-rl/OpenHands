import logging
import os
import tempfile
from pathlib import Path
from zipfile import ZipFile

import tenacity
from runloop_api_client import APIStatusError, Runloop
from runloop_api_client.types.devbox_create_params import LaunchParameters

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

        assert config.runloop_api_key, 'Runloop API key is required'
        self.config = config

        self.api_client = Runloop(
            bearer_token=config.runloop_api_key,
            base_url='https://api.runloop.pro',
        )
        self.devbox = self.api_client.devboxes.create(
            name=sid,
            launch_parameters=LaunchParameters(keep_alive_time_seconds=60 * 2),
            setup_commands=[
                f'sudo mkdir -p {config.workspace_mount_path_in_sandbox} && '
                f'sudo chown user:user {config.workspace_mount_path_in_sandbox}',
            ],
            extra_body={'prebuilt': 'public.ecr.aws/c1r6f8a9/prebuilts:allhands'},
        )
        self.shell_name = 'allhands'

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(60),
        wait=tenacity.wait_fixed(0.75),
    )
    def _wait_until_alive(self):
        """Pull devbox status until it is running"""
        if self.devbox.status == 'running':
            return

        devbox = self.api_client.devboxes.retrieve(id=self.devbox.id)
        print(f'Devbox status: {devbox.status}')

        if devbox.status != 'running':
            raise ConnectionRefusedError('Devbox is not running')

        self.api_client.devboxes.execute_sync(
            id=self.devbox.id,
            command=f'cd {self.config.workspace_mount_path_in_sandbox}',
            shell_name=self.shell_name,
        )

        # Devbox is connected and running
        self.devbox = devbox

    def run_ipython(self, action: IPythonRunCellAction) -> Observation:
        raise NotImplementedError

    def read(self, action: FileReadAction) -> Observation:
        self._wait_until_alive()

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
        self._wait_until_alive()

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
        self._wait_until_alive()

        print(f'Copying {host_src} to {sandbox_dest}')
        if recursive:
            # For recursive copy, create a zip file
            tmp_zip_file_path = f'/tmp/{sandbox_dest}.zip'
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
                    path=tmp_zip_file_path,
                )

                self.api_client.devboxes.execute_sync(
                    id=self.devbox.id,
                    command=f'unzip /tmp/uploaded.zip -d {sandbox_dest} && rm {tmp_zip_file_path}',
                )

        else:
            host_path = Path(host_src)
            if host_path.is_dir():
                raise ValueError('Recursive copy is required for directories')

            mkdir_resp = self.api_client.devboxes.execute_sync(
                id=self.devbox.id, command=f'mkdir -p {sandbox_dest}'
            )
            if mkdir_resp.exit_status != 0:
                raise Exception(f'Error creating directory: {mkdir_resp.stdout}')

            self.api_client.devboxes.upload_file(
                id=self.devbox.id,
                file=host_path,
                path=f'{sandbox_dest}/{host_path.name}',
            )

    def list_files(self, path: str | None = None) -> list[str]:
        self._wait_until_alive()

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
        self._wait_until_alive()

        # TODO: make this async vs sync
        # DO we kill? where do we manage timeout. etc
        try:
            result = self.api_client.devboxes.execute_sync(
                id=self.devbox.id,
                command=action.command,
                shell_name=self.shell_name,
            )
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
