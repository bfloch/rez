"""
CMake-based build system
"""
from __future__ import print_function

from rez.build_system import BuildSystem
from rez.build_process_ import BuildType
from rez.resolved_context import ResolvedContext
from rez.exceptions import BuildSystemError
from rez.util import create_forwarding_script
from rez.packages_ import get_developer_package
from rez.utils.platform_ import platform_
from rez.utils.system import popen
from rez.config import config
from rez.backport.shutilwhich import which
from rez.vendor.schema.schema import Or
from rez.vendor.six import six
from rez.shells import create_shell
import functools
import os.path
import sys
import os
from subprocess import PIPE


basestring = six.string_types[0]


class RezCMakeError(BuildSystemError):
    pass

class CMakeBuildSystem(BuildSystem):
    """The CMake build system.

    The 'cmake' executable is run within the build environment. Rez supplies a
    library of cmake macros in the 'cmake_files' directory; these are added to
    cmake's searchpath and are available to use in your own CMakeLists.txt
    file.

    The following CMake variables are available:
    - REZ_BUILD_TYPE: One of 'local', 'central'. Describes whether an install
      is going to the local packages path, or the release packages path.
    - REZ_BUILD_INSTALL: One of 0 or 1. If 1, an installation is taking place;
      if 0, just a build is occurring.
    """

    # Populated by build_systems()
    _default_build_system = None

    @classmethod
    def default_build_system(cls):
        # Will only evaluate once
        cls.build_systems()
        return cls._default_build_system


    _build_systems = None

    @classmethod
    def build_systems(cls):
        if cls._build_systems is not None:
            return cls._build_systems

        settings = config.plugins.build_system.cmake
        found_exe = cls._find_cmake()

        cmd = [found_exe, '--help']

        stdout = ""
        p = popen(cmd, universal_newlines=True, stdout=PIPE, stderr=PIPE)
        stdout, _ = p.communicate()

        is_in_generators = False
        generators = []
        for line in stdout.split("\n"):
            if is_in_generators:
                if "=" in line:
                    name, _ = line.split("=")

                    # Some targets may have option [arch].
                    # Not parsing this information as newer generators
                    # (Visual Studio 2019) use the -A option instead.
                    name = name.replace("[arch]", "")

                    # New cmake version highlight default with *
                    is_default = False
                    if name.startswith("* "):
                        is_default = True
                        name = name[2:]

                    name = name.strip(" ")

                    if is_default:
                        cls._default_build_system = name

                    generators.append(name)
            elif line.startswith("Generators"):
                is_in_generators = True

        # TODO: Clean up after legacy has been deprecated
        # In order to be compatible with the legacy_build_systems the keys
        # and values are mirrored

        cls._build_systems = dict(zip(generators, generators))

        return cls._build_systems

    # DEPRECATED: Maintenance burdon, since the generators depend on platorm,
    # and cmake version.
    legacy_build_systems = {
            'eclipse': "Eclipse CDT4 - Unix Makefiles",
            'codeblocks': "CodeBlocks - Unix Makefiles",
            'make': "Unix Makefiles",
            'nmake': "NMake Makefiles",
            'mingw': "MinGW Makefiles",
            'xcode': "Xcode"
        }

    build_targets = ["Debug", "Release", "RelWithDebInfo"]

    schema_dict = {
        "build_target": Or(*build_targets),
        "build_system": Or(None, basestring),
        "cmake_args": [basestring],
        "cmake_binary": Or(None, basestring),
        "make_binary": Or(None, basestring)
    }

    @classmethod
    def name(cls):
        return "cmake"

    @classmethod
    def child_build_system(cls):
        return "make"

    @classmethod
    def is_valid_root(cls, path, package=None):
        return os.path.isfile(os.path.join(path, "CMakeLists.txt"))

    @classmethod
    def bind_cli(cls, parser, group):
        settings = config.plugins.build_system.cmake
        group.add_argument("--bt", "--build-target", dest="build_target",
                           type=str, choices=cls.build_targets,
                           default=settings.build_target,
                           help="set the build target (default: %(default)s).")
        group.add_argument("--bs", "--cmake-build-system",
                           dest="cmake_build_system",
                           choices=cls.build_systems().keys(),
                           default=settings.build_system,
                           help="set the cmake build system (default: %(default)s).")

    def __init__(self, working_dir, opts=None, package=None, write_build_scripts=False,
                 verbose=False, build_args=[], child_build_args=[]):
        super(CMakeBuildSystem, self).__init__(
            working_dir,
            opts=opts,
            package=package,
            write_build_scripts=write_build_scripts,
            verbose=verbose,
            build_args=build_args,
            child_build_args=child_build_args)

        self.settings = self.package.config.plugins.build_system.cmake
        self.build_target = getattr(opts, "build_target", self.settings.build_target)
        self.cmake_build_system = getattr(opts, "cmake_build_system", self.settings.build_system)

        if self.cmake_build_system == 'xcode' and platform_.name != 'osx':
            raise RezCMakeError("Generation of Xcode project only available "
                                "on the OSX platform")

    def build(self, context, variant, build_path, install_path, install=False,
              build_type=BuildType.local):
        def _pr(s):
            if self.verbose:
                print(s)

        found_exe = self._find_cmake(context)

        sh = create_shell()

        # assemble cmake command
        cmd = [found_exe, "-d", self.working_dir]
        cmd += (self.settings.cmake_args or [])
        cmd += (self.build_args or [])

        cmd.append("-DCMAKE_INSTALL_PREFIX=%s" % install_path)
        cmd.append("-DCMAKE_MODULE_PATH=%s" %
                   sh.get_key_token("CMAKE_MODULE_PATH").replace('\\', '/')) # Replace won't work here ... needs to expand
        cmd.append("-DCMAKE_BUILD_TYPE=%s" % self.build_target)
        cmd.append("-DREZ_BUILD_TYPE=%s" % build_type.name)
        cmd.append("-DREZ_BUILD_INSTALL=%d" % (1 if install else 0))
        if self.cmake_build_system:
            generator = None
            if not self.cmake_build_system in self.build_systems().keys():
                # TODO: Deprecate
                generator = self.legacy_build_systems[self.cmake_build_system]
            else:
                generator = self.build_systems()[self.cmake_build_system]

            cmd.extend(["-G", generator])
        elif self.default_build_system():
            _pr("Using default Generator {}".format(self.default_build_system()))

        if config.rez_1_cmake_variables and \
                not config.disable_rez_1_compatibility and \
                build_type == BuildType.central:
            cmd.append("-DCENTRAL=1")

        # execute cmake within the build env
        _pr("Executing: %s" % ' '.join(cmd))
        if not os.path.abspath(build_path):
            build_path = os.path.join(self.working_dir, build_path)
            build_path = os.path.realpath(build_path)

        callback = functools.partial(self._add_build_actions,
                                     context=context,
                                     package=self.package,
                                     variant=variant,
                                     build_type=build_type,
                                     install=install,
                                     build_path=build_path,
                                     install_path=install_path)

        # run the build command and capture/print stderr at the same time
        retcode, _, _ = context.execute_shell(command=cmd,
                                              block=True,
                                              cwd=build_path,
                                              actions_callback=callback)
        ret = {}
        if retcode:
            ret["success"] = False
            return ret

        if self.write_build_scripts:
            # write out the script that places the user in a build env, where
            # they can run make directly themselves.
            build_env_script = os.path.join(build_path, "build-env")
            create_forwarding_script(build_env_script,
                                     module=("build_system", "cmake"),
                                     func_name="_FWD__spawn_build_shell",
                                     working_dir=self.working_dir,
                                     build_path=build_path,
                                     variant_index=variant.index,
                                     install=install,
                                     install_path=install_path)
            ret["success"] = True
            ret["build_env_script"] = build_env_script
            return ret

        # assemble make command
        make_binary = self.settings.make_binary

        cmd = []
        if not make_binary:
            cmd = [found_exe, "--build", build_path]
            # if self.cmake_build_system == "mingw":
            #     make_binary = "mingw32-make"
            # elif self.cmake_build_system == "nmake":
            #     make_binary = "nmake"
            # else:
            #     make_binary = "make"
        elif make_binary != "nmake":
            cmd = [make_binary]
            if not any(x.startswith("-j") for x in
                       (self.child_build_args or [])):
                n = variant.config.build_thread_count
                cmd.append("-j%d" % n)

        cmd += (self.child_build_args or [])

        all_cmd = cmd[:]     # Copy
        if not make_binary:
            if platform_.name == "windows":
                all_cmd += ["--target", "ALL_BUILD"]
            else:
                all_cmd += ["--target", "all"]

        # execute make within the build env
        _pr("\nExecuting: %s" % ' '.join(cmd))
        retcode, _, _ = context.execute_shell(command=all_cmd,
                                              block=True,
                                              cwd=build_path,
                                              actions_callback=callback)

        if not retcode and install and "install" not in cmd:
            if not make_binary:
                cmd.append("--target")
            cmd.append("install")

            # execute make install within the build env
            _pr("\nExecuting: %s" % ' '.join(cmd))
            retcode, _, _ = context.execute_shell(command=cmd,
                                                  block=True,
                                                  cwd=build_path,
                                                  actions_callback=callback)

        ret["success"] = (not retcode)
        return ret

    @classmethod
    def _find_cmake(cls, context=None):
        settings = config.plugins.build_system.cmake
        exe = None

        if settings.cmake_binary:
            exe = settings.cmake_binary
        elif context:
            exe = context.which("cmake", fallback=True)
        else:
            # No context. Try system path.
            exe = "cmake"
        if not exe:
            raise RezCMakeError("could not find cmake binary")
        found_exe = which(exe)
        if not found_exe:
            raise RezCMakeError("cmake binary does not exist: %s" % exe)
        return found_exe

    @classmethod
    def _add_build_actions(cls, executor, context, package, variant,
                           build_type, install, build_path, install_path=None):
        settings = package.config.plugins.build_system.cmake
        cmake_path = os.path.join(os.path.dirname(__file__), "cmake_files")
        template_path = os.path.join(os.path.dirname(__file__), "template_files")

        cls.set_standard_vars(executor=executor,
                              context=context,
                              variant=variant,
                              build_type=build_type,
                              install=install,
                              build_path=build_path,
                              install_path=install_path)

        executor.env.CMAKE_MODULE_PATH.append(cmake_path.replace('\\', '/'))
        executor.env.REZ_BUILD_DOXYFILE = os.path.join(template_path, 'Doxyfile')
        executor.env.REZ_BUILD_INSTALL_PYC = '1' if settings.install_pyc else '0'


def _FWD__spawn_build_shell(working_dir, build_path, variant_index, install,
                            install_path=None):
    # This spawns a shell that the user can run 'make' in directly
    context = ResolvedContext.load(os.path.join(build_path, "build.rxt"))
    package = get_developer_package(working_dir)
    variant = package.get_variant(variant_index)
    config.override("prompt", "BUILD>")

    callback = functools.partial(CMakeBuildSystem._add_build_actions,
                                 context=context,
                                 package=package,
                                 variant=variant,
                                 build_type=BuildType.local,
                                 install=install,
                                 build_path=build_path,
                                 install_path=install_path)

    retcode, _, _ = context.execute_shell(block=True,
                                          cwd=build_path,
                                          actions_callback=callback)
    sys.exit(retcode)


def register_plugin():
    return CMakeBuildSystem


# Copyright 2013-2016 Allan Johns.
#
# This library is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.
