# container-build
`container-build` is a tool to run a command within a generated container, geared toward setting up reproducible
enviroments for build systems. Almost any linux-based base image should work, but `apt` is currently the only supported
package manager. Docker is currently the only supported container backend.

Options may be configured using command-line arguments or config files. By default, `container-build` will create a user
and group within the container named `build` with the same uid and gid it is run with, mount the current working
directory under `/home/build/src` in the container, and run the given command as the generated user. Additionally, some
configuration files automatically detected under the current working directory trigger additional behaviour:

  * `container-build/build.cfg` may specify any command line arguments in an ini-style format.
  * `container-build/sources.list` will be installed as an `apt` sources file in the container.
  * `container-bulid/apt-keys/*.gpg` will be added to the `apt` trusted gpg key database in the container.
  * `container-build/packages` specifies packages to be installed in the container, one per line.
  * `container-build/install.sh` will be run as root while building the container image, after package installation.
  * `container-build/user_install.sh` will be run as the build user while building the container image, as a final step.

## Running

```
$ python3 container_build.py --help
```
