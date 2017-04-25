import subprocess
import uuid
import yaml
from contextlib import ContextDecorator
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory, mkdtemp
from typing import Dict, List, Optional, Set, Tuple

from docker import Client


class _Node:
    """
    A record of a DC/OS cluster node.
    """

    def __init__(self, ip_address: str, ssh_key_path: Path) -> None:
        """
        Args:
            ip_address: The IP address of the node.
            ssh_key_path: The path to an SSH key which can be used to SSH to
                the node as the `root` user.
        """
        self._ip_address = ip_address
        self._ssh_key_path = ssh_key_path

    def run_as_root(self, args: List[str]) -> subprocess.CompletedProcess:
        """
        Run a command on this node as ``root``.

        Args:
            args: The command to run on the node.

        Returns:
            The representation of the finished process.

        Raises:
            CalledProcessError: The process exited with a non-zero code.
        """
        ssh_args = [
            'ssh',
            # Suppress warnings.
            # In particular, we don't care about remote host identification
            # changes.
            "-q",
            # The node may be an unknown host.
            "-o",
            "StrictHostKeyChecking=no",
            # Use an SSH key which is authorized.
            "-i",
            str(self._ssh_key_path),
            # Run commands as the root user.
            "-l",
            "root",
            # Bypass password checking.
            "-o",
            "PreferredAuthentications=publickey",
            self._ip_address,
        ] + args

        return subprocess.run(args=ssh_args, check=True)


class _DCOS_Docker:
    """
    A record of a DC/OS Docker cluster.
    """

    def __init__(
        self,
        masters: int,
        agents: int,
        public_agents: int,
        extra_config: Dict,
        generate_config_path: Optional[Path],
        dcos_docker_path: Path,
    ) -> None:
        """
        Create a DC/OS Docker cluster.

        Args:
            masters: The number of master nodes to create.
            agents: The number of master nodes to create.
            public_agents: The number of master nodes to create.
            extra_config: DC/OS Docker comes with a "base" configuration.
                This dictionary can contain extra installation configuration
                variables.
            generate_config_path: The path to a build artifact to install.
            dcos_docker_path: The path to a clone of DC/OS Docker.
        """
        self._masters = masters
        self._agents = agents
        self._public_agents = public_agents
        self._path = dcos_docker_path

        # If we run tests concurrently, we want unique container names.
        # However, it may be useful for debugging to group these names.
        # Therefore, we use the same random string in each container in a
        # cluster.
        random = uuid.uuid4()

        master_container_name = (
            'dcos-docker-master-test-{random}-'.format(random=random)
        )
        agent_container_name = (
            'dcos-docker-agent-test-{random}-'.format(random=random)
        )
        public_agent_container_name = (
            'dcos-docker-public-agent-test-{random}-'.format(random=random)
        )

        config_dir = TemporaryDirectory(dir='/tmp')
        config_path = Path(config_dir.name) / 'dcos_generate_config.sh'
        copyfile(src=str(generate_config_path), dst=str(config_path))
        self._ssh_dir = Path(mkdtemp(dir='/tmp'))

        self._variables = {
            'MASTERS': str(masters),
            'AGENTS': str(agents),
            'PUBLIC_AGENTS': str(public_agents),
            'MASTER_CTR': master_container_name,
            'AGENT_CTR': agent_container_name,
            'PUBLIC_AGENT_CTR': public_agent_container_name,
            'SSH_DIR': str(self._ssh_dir),
        }  # type: Dict[str, str]

        if extra_config:
            self._variables['EXTRA_GENCONF_CONFIG'] = yaml.dump(
                data=extra_config,
                default_flow_style=False,
            )

        self._variables['DCOS_GENERATE_CONFIG_PATH'] = str(config_path)

        self._make(variables=self._variables, target='all')

    def _make(self, variables: Dict[str, str], target: str) -> None:
        """
        Run `make` in the DC/OS Docker directory.

        Args:
            variables: Variables to pass to `make`.
            target: `make` target to run.

        Raises:
            CalledProcessError: The process exited with a non-zero code.
        """
        args = ['make'] + [
            '{key}={value}'.format(key=key, value=value)
            for key, value in self._variables.items()
        ] + [target]

        subprocess.check_output(args=args, cwd=str(self._path))

    def postflight(self) -> None:
        """
        Wait for nodes to be ready to run tests against.
        """
        self._make(variables={}, target='postflight')

    def destroy(self) -> None:
        """
        Destroy all nodes in the cluster.
        """
        self._make(variables={}, target='clean')

    def _nodes(self, container_base_name: str, num_nodes: int) -> Set[_Node]:
        """
        Args:
            container_base_name: The start of the container names.
            num_nodes: The number of nodes.

        Returns: ``_Node``s corresponding to containers with names starting
            with ``container_base_name``.
        """
        client = Client()
        nodes = set([])  # type: Set[_Node]

        while len(nodes) < num_nodes:
            container_name = '{container_base_name}{number}'.format(
                container_base_name=container_base_name,
                number=len(nodes) + 1,
            )
            details = client.inspect_container(container=container_name)
            ip_address = details['NetworkSettings']['IPAddress']
            node = _Node(
                ip_address=ip_address,
                ssh_key_path=self._ssh_dir / 'id_rsa',
            )
            nodes.add(node)

        return nodes

    @property
    def masters(self) -> Set[_Node]:
        """
        Return all DC/OS master ``_Node``s.
        """
        return self._nodes(
            container_base_name=self._variables['MASTER_CTR'],
            num_nodes=self._masters,
        )


class Cluster(ContextDecorator):
    """
    A record of a DC/OS Cluster.

    This is intended to be used as context manager.
    """

    def __init__(
        self,
        extra_config: Dict,
        masters: int=1,
        agents: int=0,
        public_agents: int=0,
    ) -> None:
        """
        Args:
            extra_config: This dictionary can contain extra installation
                configuration variables to add to base configurations.
            masters: The number of master nodes to create.
            agents: The number of master nodes to create.
            public_agents: The number of master nodes to create.
        """
        # See README.md for information on the required configuration.
        with open('configuration.yaml') as configuration:
            tests_config = yaml.load(configuration)

        self._backend = _DCOS_Docker(
            masters=masters,
            agents=agents,
            public_agents=public_agents,
            extra_config=extra_config,
            generate_config_path=Path(
                tests_config['dcos_generate_config_path']),
            dcos_docker_path=Path(tests_config['dcos_docker_path']),
        )
        self._backend.postflight()

    def __enter__(self) -> 'Cluster':
        """
        A context manager receives this ``Cluster`` instance.
        """
        return self

    @property
    def masters(self) -> Set[_Node]:
        """
        Return all DC/OS master ``_Node``s.
        """
        return self._backend.masters

    def __exit__(self, *exc: Tuple[None, None, None]) -> bool:
        """
        On exiting, destroy all nodes in the cluster.
        """
        self._backend.destroy()
        return False
