import threading
import uuid
from typing import Callable, Optional

import requests
import tenacity
from runloop_api_client import Runloop
from runloop_api_client.types.shared_params import LaunchParameters

from openhands.core.config import AppConfig
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.remote.runtime import RemoteRuntime
from openhands.runtime.runtime import Runtime


class RunloopRuntime2(RemoteRuntime):
    port: int = 60000  # default port for the remote runtime client

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_message_callback: Optional[Callable] = None,
    ):
        # Initialize runloop api client
        self.config = config
        self.runloop_api_client = Runloop(
            bearer_token=config.runloop_api_key,
            base_url='https://api.runloop.pro',
        )

        self.status_message_callback = status_message_callback
        self.send_status_message('STATUS$STARTING_RUNTIME')
        self.session = requests.Session()
        # self.session.headers.update({'X-API-Key': self.config.sandbox.api_key})
        self.action_semaphore = threading.Semaphore(1)

        if self.config.workspace_base is not None:
            logger.warning(
                'Setting workspace_base is not supported in the remote runtime.'
            )

        self.runtime_id: str | None = None
        self.runtime_url: str | None = None

        self.instance_id = (
            sid + str(uuid.uuid4()) if sid is not None else str(uuid.uuid4())
        )
        self.container_name = 'oh-remote-runtime-' + self.instance_id

        # Prepare the request body for the /start endpoint
        plugin_arg = ''
        if plugins is not None and len(plugins) > 0:
            plugin_arg = f'--plugins {" ".join([plugin.name for plugin in plugins])} '
        browsergym_arg = (
            f'--browsergym-eval-env {self.config.sandbox.browsergym_eval_env}'
            if self.config.sandbox.browsergym_eval_env is not None
            else ''
        )

        start_command = (
            f'/openhands/micromamba/bin/micromamba run -n openhands '
            'poetry run '
            f'python -u -m openhands.runtime.client.client {self.port} '
            f'--working-dir {self.config.workspace_mount_path_in_sandbox} '
            f'{plugin_arg}'
            f'--username {"openhands" if self.config.run_as_openhands else "root"} '
            f'--user-id {self.config.sandbox.user_id} '
            f'{browsergym_arg}'
        )

        self.send_status_message('STATUS$WAITING_FOR_CLIENT')
        start_command = (
            'export MAMBA_ROOT_PREFIX=/openhands/micromamba && '
            'cd /openhands/code && '
            + '/openhands/micromamba/bin/micromamba run -n openhands poetry config virtualenvs.path /openhands/poetry && '
            + '/openhands/micromamba/bin/micromamba run -n openhands poetry run playwright install --with-deps chromium && '
            + start_command
        )
        print('startCommand=', start_command)
        # self.devbox = self.runloop_api_client.devboxes.retrieve(id="dbx_2y48vzgyeGm0wJcgrOY9M")
        # self.devbox = None
        self.devbox = self.runloop_api_client.devboxes.create(
            entrypoint=start_command,
            name=self.container_name,
            environment_variables={'DEBUG': 'true'} if self.config.debug else {},
            prebuilt='openhands',
            launch_parameters=LaunchParameters(
                keep_alive_time_seconds=config.sandbox.timeout,
                available_ports=[self.port],
            ),
        )

        # NOTE: Copied from RemoteRuntime, is this necessary?
        self._wait_for_devbox()

        # Create tunnel
        tunnel = self.runloop_api_client.devboxes.create_tunnel(
            id=self.devbox.id,
            port=self.port,
        )

        self.runtime_id = self.devbox.id  # self.devbox.id
        self.runtime_url = f'https://{tunnel.url}'
        print(f'created devbox.id={self.devbox.id}')

        logger.info(
            f'Sandbox started. Runtime ID: {self.runtime_id}, URL: {self.runtime_url}'
        )

        # NOTE: Avoid calling RemoteRuntime for now as API key is not set
        Runtime.__init__(
            self, config, event_stream, sid, plugins, env_vars, status_message_callback
        )

        logger.info(
            f'Runtime initialized with plugins: {[plugin.name for plugin in self.plugins]}'
        )
        logger.info(f'Runtime initialized with env vars: {env_vars}')
        assert (
            self.runtime_id is not None
        ), 'Runtime ID is not set. This should never happen.'
        assert (
            self.runtime_url is not None
        ), 'Runtime URL is not set. This should never happen.'

        self._wait_until_alive()

        self.send_status_message(' ')

        self._wait_until_alive()
        self.setup_initial_env()

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(90),
        wait=tenacity.wait_fixed(0.75),
    )
    def _wait_for_devbox(self):
        """Pull devbox status until it is running"""
        print(f'devbox.state={self.devbox.status}')

        if self.devbox.status == 'running':
            return

        devbox = self.runloop_api_client.devboxes.retrieve(id=self.devbox.id)
        print(f'devbox.state={devbox.status}')
        if devbox.status != 'running':
            raise ConnectionRefusedError('Devbox is not running')

        # Devbox is connected and running
        self.devbox = devbox

    def close(self, timeout: int = 10):
        pass
        # if self.devbox:
        #     self.runloop_api_client.devboxes.shutdown(self.devbox.id)
