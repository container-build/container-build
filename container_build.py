#!/usr/bin/env python3

import argparse, configparser, contextlib, tempfile, os, re, shlex, shutil, subprocess, sys
from pathlib import Path
from subprocess import CalledProcessError
from urllib.parse import urlparse

CONFIG_DIRECTORY = 'container-build'

DEFAULT_APT_KEYS         = str(Path(CONFIG_DIRECTORY, 'apt-keys'))
DEFAULT_APT_SOURCES_FILE = str(Path(CONFIG_DIRECTORY, 'sources.list'))
DEFAULT_BASE_IMAGE       = 'debian:stretch-slim'
DEFAULT_CONFIG_FILE      = str(Path(CONFIG_DIRECTORY, 'build.cfg'))
DEFAULT_DOCKER           = 'docker'
DEFAULT_DOCKER_HOST      = 'unix:///var/run/docker.sock'
DEFAULT_DOCKER_RUN_FLAGS = '--interactive --tty --rm --env LC_ALL=C.UTF-8'
DEFAULT_HOME_DIR         = '/home/build'
DEFAULT_INSTALL_SCRIPT   = str(Path(CONFIG_DIRECTORY, 'install.sh'))
DEFAULT_PACKAGES_FILE    = str(Path(CONFIG_DIRECTORY, 'packages'))
DEFAULT_SHELL            = '/bin/bash'
DEFAULT_USERNAME         = 'build'
DEFAULT_WORK_DIR         = 'src'

SCRIPTS_DIR = 'scripts'
APT_KEYS_DIR = 'apt-keys'


def main():
    args = arg_parser().parse_args()
    config = ConfigMerger(args)
    opts = Options(config)

    if opts.verbose >= 1 and config.config_file is not None:
        print(f'Read config file {config.config_file}', file=sys.stderr)

    try:
        packages = read_packages(opts.packages_file)
    except OSError as ex:
        print(f'Error opening packages file \'{opts.packages_file}\': {ex}', file=sys.stderr)
        exit(1)

    if opts.package:
        for package_arg in opts.package:
            for package in re.split(r'\s+', package_arg):
                if package:
                    packages.append(package)

    apt_sources_src = opts.apt_sources_file
    if opts.apt_sources_file is not None:
        apt_sources = Path(opts.apt_sources_file).name
    else:
        apt_sources = None

    if opts.apt_keys is not None:
        apt_keys_src = []
        apt_keys = []
        for apt_key_name in os.listdir(opts.apt_keys):
            apt_keys_src.append(Path(opts.apt_keys, apt_key_name))
            apt_keys.append(Path(APT_KEYS_DIR, apt_key_name))
    else:
        apt_keys_src = None
        apt_keys = None

    install_scripts_src = opts.install_script
    install_scripts = []
    for (install_script_index, install_script_src) in enumerate(install_scripts_src or []):
        install_script_name = Path(install_script_src).name
        install_scripts.append(Path(SCRIPTS_DIR, f'{install_script_index}_{install_script_name}'))

    work_dir = str(Path(opts.home_dir, opts.work_dir))

    groups = []

    if opts.uid == 0 or opts.gid == 0:
        print('Cannot run command as root in container (use the --uid and --gid arguments).', file=sys.stderr)
        exit(1)

    try:
        volumes = collect_volumes(opts.mount, work_dir, not opts.no_recursive_mount)
    except FileNotFoundError as ex:
        print(f'Error resolving mount path: {ex}', file=sys.stderr)
        exit(1)

    if opts.docker_passthrough:
        try:
            docker_host = urlparse(opts.docker_host)
            if docker_host.scheme != 'unix':
                print(f'Passthrough of daemon socket scheme \'{docker_host.scheme}\' not supported', file=sys.stderr)
                exit(1)

            passthrough_sock_dst = Path(docker_host.path)
            passthrough_sock_src = passthrough_sock_dst.resolve()
            volumes[str(passthrough_sock_src)] = str(passthrough_sock_dst)

            passthrough_sock_stat = passthrough_sock_src.stat()
            if passthrough_sock_stat.st_uid != opts.uid:
                if passthrough_sock_stat.st_mode & 0o060 != 0o060:
                    print(f'Passthrough of daemon socket \'{docker_host}\' not writable by group owner unsupported',
                          file=sys.stderr)
                    exit(1)
                if passthrough_sock_stat.st_gid == 0:
                    print(f'Passthrough of daemon socket \'{docker_host}\' owned by group 0 not supported',
                          file=sys.stderr)
                    exit(1)
                groups.append(str(passthrough_sock_stat.st_gid))
        except (OSError, KeyError) as ex:
            print(f'Error fetching passthrough socket path: {ex}', file=sys.stderr)
            exit(1)

    dockerfile_data = generate_dockerfile(
        base_image=opts.base_image,
        username=opts.username,
        home_dir=opts.home_dir,
        shell=opts.shell,
        work_dir=work_dir,
        apt_sources=apt_sources,
        apt_keys=apt_keys,
        packages=packages,
        install_scripts=install_scripts,
    )

    with create_build_dir(opts.directory) as build_dir:
        dockerfile_path = Path(build_dir, 'Dockerfile')

        try:
            with open(dockerfile_path, mode='w', encoding='utf-8') as dockerfile:
                dockerfile.write(dockerfile_data)
        except OSError as ex:
            print(f'error writing Dockerfile at {dockerfile_path}: {ex}', file=sys.stderr)
            exit(1)

        if opts.verbose >= 1:
            print(f'wrote Dockerfile at {dockerfile_path}:\n{dockerfile_data}', file=sys.stderr)

        build_src_dsts = []
        if apt_sources_src is not None:
            build_src_dsts.append((apt_sources_src, apt_sources))
        if apt_keys_src is not None:
            build_src_dsts.extend(zip(apt_keys_src, apt_keys))
        if install_scripts_src is not None:
            build_src_dsts.extend(zip(install_scripts_src, install_scripts))
        if not copy_build_files(build_src_dsts, build_dir, opts.verbose):
            exit(1)

        run_docker_result = run_docker(
            docker=opts.docker,
            docker_run_flags=opts.docker_run_flags,
            image_name=opts.image_name,
            build_dir=build_dir,
            dockerfile_path=dockerfile_path,
            uid=opts.uid,
            gid=opts.gid,
            groups=groups,
            volumes=volumes.items(),
            command=opts.command,
            verbose=opts.verbose,
        )
        if not run_docker_result:
            exit(1)


def infer_name():
    dir_name = Path.cwd().name
    return f'{dir_name}-builder'


def read_packages(packages_path):
    with open(packages_path, mode='r', encoding='utf-8') as packages_file:
        packages = []
        for package in re.split(r'\s+', packages_file.read()):
            if package:
                packages.append(package)
        return packages


def collect_volumes(mount_args, work_dir, recursive):
    volumes = {}
    for mount_arg_str in mount_args:
        mount_arg = Path(mount_arg_str)
        mount_src = mount_arg.resolve()
        mount_dst = Path(work_dir, mount_arg.name)
        volumes[str(mount_src)] = str(mount_dst)
        if recursive and mount_src.is_dir():
            with os.scandir(mount_src) as subdirs:
                for subdir in subdirs:
                    if subdir.is_symlink() and subdir.is_dir():
                        subdir_src = Path(subdir.path).resolve()
                        subdir_dst = Path(mount_dst, subdir.name)
                        volumes[str(subdir_src)] = str(subdir_dst)
    return volumes


def create_build_dir(directory):
    if directory is None:
        return tempfile.TemporaryDirectory()

    os.makedirs(directory, mode=0o755, exist_ok=True)

    @contextlib.contextmanager
    def directory_contextmanager():
        yield directory

    return directory_contextmanager()


def arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='A tool to run a command within a generated container, geared toward build systems.',
        epilog=f'''\
environment variables:
  DOCKER                Path to docker executable. Defaults to '{DEFAULT_DOCKER}'.
  DOCKER_RUN_FLAGS      Extra flags to pass to 'docker run' command. Defaults to '{DEFAULT_DOCKER_RUN_FLAGS}'.
  DOCKER_HOST           Docker daemon socket to connect to. Defaults to '{DEFAULT_DOCKER_HOST}'.

The config file is in ini-style format and can contain any long-form command line argument or environment variable. Only
the first section in the config file is used, and the name of the generated container will default to the name of that
section.'''
    )
    parser.add_argument('command', nargs='*',
                        help='Command to run within the container.')
    parser.add_argument('-c', '--config-file',
                        help=f'Path of config file. Defaults to \'{DEFAULT_CONFIG_FILE}\', if it exists.')
    parser.add_argument('--no-config-file',
                        help=f'Suppress using default config file path \'{DEFAULT_CONFIG_FILE}\'.')
    parser.add_argument('-n', '--name',
                        help='Name of generated container image. Defaults to the name of the current working'
                        ' directory suffixed with \'-builder\'.')
    parser.add_argument('-d', '--directory',
                        help='Path to directory to write generated files. Defaults to using a temporary directory.')
    parser.add_argument('--install-script', action='append',
                        help='Path of extra script to run as root in container during image creation. May be specified'
                        f' multiple times. Defaults to \'{DEFAULT_INSTALL_SCRIPT}\', if it exists.')
    parser.add_argument('--no-install-script', action='store_const', const=True,
                        help=f'Suppress using the default install script path \'{DEFAULT_INSTALL_SCRIPT}\'.')
    parser.add_argument('--base-image',
                        help=f'Base image to derive the container from. Defaults to \'{DEFAULT_BASE_IMAGE}\'.')
    parser.add_argument('-p', '--package', action='append',
                        help='Apt package specification of package to install in the container. May be specified'
                        ' multiple times.')
    parser.add_argument('--packages-file',
                        help='Path of file containing apt package specifications to install in the container. Defaults'
                        f' to \'{DEFAULT_PACKAGES_FILE}\'.')
    parser.add_argument('--apt-sources-file',
                        help='Path of apt sources.list to use during package installation in the container. Defaults to'
                        f' \'{DEFAULT_APT_SOURCES_FILE}\', if it exists.')
    parser.add_argument('--no-apt-sources-file', action='store_const', const=True,
                        help=f'Suppress using the default apt sources path \'{DEFAULT_APT_SOURCES_FILE}\'.')
    parser.add_argument('--apt-keys',
                        help='Path of directory containing .gpg files to install using apt-key in the container.'
                        f' Defaults to \'{DEFAULT_APT_KEYS}\', if it exists.')
    parser.add_argument('--no-apt-keys', action='store_const', const=True,
                        help=f'Suppress using the default apt keys path \'{DEFAULT_APT_KEYS}\'.')
    parser.add_argument('-u', '--uid', type=int,
                        help='UID used to run COMMAND in the container. Defaults to current euid.')
    parser.add_argument('-g', '--gid', type=int,
                        help='GID used to run COMMAND in the container. Defaults to current egid.')
    parser.add_argument('--username',
                        help=f'Username used to run COMMAND in the container. Defaults to \'{DEFAULT_USERNAME}\'.')
    parser.add_argument('--home-dir',
                        help=f'Path of home directory used in the container. Defaults to \'{DEFAULT_HOME_DIR}\'.')
    parser.add_argument('--shell',
                        help=f'Path of shell used to run COMMAND in the container. Defaults to \'{DEFAULT_SHELL}\'.')
    parser.add_argument('--work-dir',
                        help='Path of working directory to run COMMAND in the container, optionally relative to the'
                        f' home directory. Defaults to \'{DEFAULT_WORK_DIR}\'.')
    parser.add_argument('-m', '--mount', action='append',
                        help='Directory to bind mount under the working directory in the container. May be specified'
                        ' multiple times. Defaults to the current directory.')
    parser.add_argument('--no-recursive-mount', action='store_const', const=True,
                        help='Suppress recursively mounting symlinks to directories outside their containing mount.')
    parser.add_argument('--docker-passthrough', action='store_const', const=True,
                        help='Mount docker unix socket from host inside container, and add user to group owning the'
                        ' socket inside the container.')
    parser.add_argument('-v', '--verbose', action='count',
                        help='Enable verbose output. May be specified multiple times for more verbosity.')
    return parser


def generate_dockerfile(base_image, username, home_dir, shell, work_dir, apt_sources, apt_keys, packages,
                        install_scripts):
    pre_packages = []
    if apt_sources:
        pre_packages.append('apt-transport-https')
    if apt_keys:
        pre_packages.extend(['gnupg', 'software-properties-common'])

    apt_keys_dst = []
    for apt_key in apt_keys:
        apt_keys_dst.append(f'/tmp/build/{apt_key}')

    dockerfile = f'''\
FROM {base_image}

ARG UID
ARG GID

'''

    if apt_keys:
        dockerfile += f'''\
COPY [ "{APT_KEYS_DIR}", "/tmp/build/{APT_KEYS_DIR}" ]

'''

    if pre_packages:
        pre_package_args = ' \\\n       '.join(pre_packages)
        dockerfile += f'''\
RUN    apt-get update \\
    && apt-get install --no-install-recommends -y \\
       {pre_package_args} \\
'''
    if apt_keys:
        assert pre_packages
        apt_key_args = ' '.join(apt_keys_dst)
        dockerfile += f'''\
    && apt-key add {apt_key_args} \\
    && rm -rf /tmp/build \\
'''

    if pre_packages:
        dockerfile += '''\
    && rm -rf /var/lib/apt/lists/*

'''

    if apt_sources:
        apt_sources_path = Path(apt_sources)
        dockerfile += f'''\
COPY [ "{apt_sources_path}", "/etc/apt/sources.list.d/build.list" ]

'''

    if packages:
        package_args = ' \\\n       '.join(packages)
        dockerfile += f'''\
RUN    apt-get update \\
    && apt-get install --no-install-recommends -y \\
       {package_args} \\
    && rm -rf /var/lib/apt/lists/*

'''

    for install_script_str in install_scripts or []:
        install_script = Path(install_script_str)
        dockerfile += f'''\
COPY [ "{install_script}", "/tmp/build/{install_script.name}" ]
RUN    '/tmp/build/{install_script.name}' \\
    && rm -rf /tmp/build

'''

    dockerfile += f'''\
# Create a user to map the host user to.
RUN    groupadd -o -g ${{GID}} '{username}' \\
    && useradd -m -o -u ${{UID}} -g ${{GID}} -s '{shell}' '{username}'
USER {username}
ENV HOME {home_dir}
ENV USER {username}
WORKDIR {work_dir}

CMD [ "{shell}" ]
'''
    return dockerfile


def copy_build_files(src_dsts, build_dir, verbose):
    for (src, dst) in src_dsts:
        full_dst = Path(build_dir, dst)
        try:
            if verbose >= 2:
                print(f'copying file \'{src}\' to build context \'{full_dst}\'', file=sys.stderr)
            os.makedirs(full_dst.parent, mode=0o755, exist_ok=True)
            shutil.copy2(src, str(full_dst))
        except OSError as ex:
            print(f'error copying file \'{src}\' to build context: {ex}', file=sys.stderr)
            return False
    return True


def run_docker(docker, docker_run_flags, image_name, build_dir, dockerfile_path, uid, gid, groups, volumes, command,
               verbose):
    try:
        docker_build_subprocess_args = {}
        docker_build_args = [
            docker, 'build',
            '--build-arg', f'UID={uid}',
            '--build-arg', f'GID={gid}',
            '--tag', image_name,
            '--file', str(dockerfile_path),
        ]

        if verbose < 1:
            docker_build_args.append('--quiet')
            docker_build_subprocess_args['stdout'] = subprocess.DEVNULL

        docker_build_args.append(str(build_dir))

        if verbose >= 1:
            print('running ' + ' '.join(docker_build_args), file=sys.stderr)

        subprocess.run(docker_build_args, check=True, **docker_build_subprocess_args)
    except CalledProcessError as ex:
        print(f'docker build returned {ex.returncode}', file=sys.stderr)
        return False

    try:
        docker_run_args = [docker, 'run']
        if len(groups) != 0:
            docker_run_args.extend(['--group-add', ','.join(groups)])
        docker_run_args.extend(shlex.split(docker_run_flags))
        for (host_dir, container_dir) in volumes:
            docker_run_args.extend(['--volume', f'{host_dir}:{container_dir}'])
        docker_run_args.append(image_name)
        docker_run_args.extend(command)

        if verbose >= 1:
            print('running ' + ' '.join(docker_run_args), file=sys.stderr)

        subprocess.run(docker_run_args, check=True)
    except CalledProcessError as ex:
        print(f'docker run returned {ex.returncode}', file=sys.stderr)
        return False
    return True


class ConfigMerger:
    def __init__(self, args):
        self.args = args
        self.config = None

        self.config_file = self.get_file('config-file', DEFAULT_CONFIG_FILE)
        if self.config_file is not None:
            config = configparser.ConfigParser(allow_no_value=True)
            config.read(self.config_file)
            if len(config.sections()) != 0:
                self.config = config
                self.config_section = config.sections()[0]

    def get(self, name, default=None):
        return self.get_or_else(name, lambda: default)

    def get_or_else(self, name, default=None):
        arg = getattr(self.args, name.replace('-', '_'), None)
        if arg is not None:
            return arg
        if self.config is not None:
            if self.config.has_option(self.config_section, name):
                config_value = self.config.get(self.config_section, name)
                if config_value is not None:
                    return config_value
                return True
        if default:
            return default()

    def get_flag(self, name):
        value = self.get(name)
        if isinstance(value, bool):
            return value
        return value is not None

    def get_list(self, name):
        value = self.get(name, default=[])
        if isinstance(value, list):
            return value
        if value is not None:
            return [value]
        return []

    def get_env(self, name, default=None):
        env = os.getenv(name)
        if env is not None:
            return env
        return self.get(name.lower(), default)

    def get_file(self, name, default):
        arg = self.get(name)
        if arg is not None:
            return arg
        if default is None or self.get(f'no-{name}'):
            return None

        if isinstance(default, list):
            present = []
            for default_path in default:
                if os.path.exists(default_path):
                    present.append(default_path)
            if present:
                return present
        else:
            if os.path.exists(default):
                return default


class Options:
    def __init__(self, config):
        self.apt_keys           = config.get_file('apt-keys', DEFAULT_APT_KEYS)
        self.apt_sources_file   = config.get_file('apt-sources-file', DEFAULT_APT_SOURCES_FILE)
        self.base_image         = config.get('base-image', DEFAULT_BASE_IMAGE)
        self.command            = config.get('command')
        self.directory          = config.get('directory')
        self.docker             = config.get_env("DOCKER", DEFAULT_DOCKER)
        self.docker_host        = config.get_env('DOCKER_HOST', DEFAULT_DOCKER_HOST)
        self.docker_passthrough = config.get_flag('docker-passthrough')
        self.docker_run_flags   = config.get_env("DOCKER_RUN_FLAGS", DEFAULT_DOCKER_RUN_FLAGS)
        self.gid                = config.get_or_else('gid', os.getegid)
        self.image_name         = config.get_or_else('name', infer_name)
        self.home_dir           = config.get('home-dir', DEFAULT_HOME_DIR)
        self.install_script     = config.get_file('install-script', [DEFAULT_INSTALL_SCRIPT])
        self.mount              = config.get('mount', ['.'])
        self.no_recursive_mount = config.get_flag('no-recursive-mount')
        self.package            = config.get_list('package')
        self.packages_file      = config.get('packages-file', DEFAULT_PACKAGES_FILE)
        self.uid                = config.get_or_else('uid', os.geteuid)
        self.username           = config.get('username', DEFAULT_USERNAME)
        self.shell              = config.get('shell', DEFAULT_SHELL)
        self.verbose            = int(config.get('verbose', 0))
        self.work_dir           = config.get('work-dir', DEFAULT_WORK_DIR)


if __name__ == '__main__':
    main()
