import os
import re
import textwrap
from collections import OrderedDict

from jinja2 import Template

from conan.tools.apple.apple import is_apple_os, to_apple_arch, get_apple_sdk_name
from conan.tools.build import build_jobs, cross_building
from conan.tools.build.flags import architecture_flag, libcxx_flag
from conan.tools.cmake.toolchain import CONAN_TOOLCHAIN_FILENAME
from conan.tools.cmake.utils import is_multi_configuration
from conan.tools.intel import IntelCC
from conan.tools.microsoft.visual import is_msvc
from conans.errors import ConanException
from conans.util.files import load


class Block(object):
    def __init__(self, conanfile, toolchain):
        self._conanfile = conanfile
        self._toolchain = toolchain
        self._context_values = None

    @property
    def values(self):
        if self._context_values is None:
            self._context_values = self.context()
        return self._context_values

    @values.setter
    def values(self, context_values):
        self._context_values = context_values

    def get_rendered_content(self):
        context = self.values
        if context is None:
            return

        def cmake_value(value):
            if isinstance(value, bool):
                return "ON" if value else "OFF"
            else:
                return '"{}"'.format(value)

        template = Template(self.template, trim_blocks=True, lstrip_blocks=True)
        template.environment.filters["cmake_value"] = cmake_value
        return template.render(**context)

    def context(self):
        return {}

    @property
    def template(self):
        raise NotImplementedError()


class VSRuntimeBlock(Block):
    template = textwrap.dedent("""
        # Definition of VS runtime, defined from build_type, compiler.runtime, compiler.runtime_type
        {% set genexpr = namespace(str='') %}
        {% for config, value in vs_runtimes.items() %}
            {% set genexpr.str = genexpr.str +
                                  '$<$<CONFIG:' + config + '>:' + value|string + '>' %}
        {% endfor %}
        set(CMAKE_POLICY_DEFAULT_CMP0091 NEW)
        set(CMAKE_MSVC_RUNTIME_LIBRARY "{{ genexpr.str }}")
        """)

    def context(self):
        # Parsing existing toolchain file to get existing configured runtimes
        settings = self._conanfile.settings
        compiler = settings.get_safe("compiler")
        if compiler not in ("Visual Studio", "msvc", "intel-cc"):
            return

        config_dict = {}
        if os.path.exists(CONAN_TOOLCHAIN_FILENAME):
            existing_include = load(CONAN_TOOLCHAIN_FILENAME)
            msvc_runtime_value = re.search(r"set\(CMAKE_MSVC_RUNTIME_LIBRARY \"([^)]*)\"\)",
                                           existing_include)
            if msvc_runtime_value:
                capture = msvc_runtime_value.group(1)
                matches = re.findall(r"\$<\$<CONFIG:([A-Za-z]*)>:([A-Za-z]*)>", capture)
                config_dict = dict(matches)

        build_type = settings.get_safe("build_type")  # FIXME: change for configuration
        if build_type is None:
            return None
        runtime = settings.get_safe("compiler.runtime")
        if compiler == "Visual Studio":
            config_dict[build_type] = {"MT": "MultiThreaded",
                                       "MTd": "MultiThreadedDebug",
                                       "MD": "MultiThreadedDLL",
                                       "MDd": "MultiThreadedDebugDLL"}[runtime]
        if compiler == "msvc" or compiler == "intel-cc":
            runtime_type = settings.get_safe("compiler.runtime_type")
            rt = "MultiThreadedDebug" if runtime_type == "Debug" else "MultiThreaded"
            if runtime != "static":
                rt += "DLL"
            config_dict[build_type] = rt
        return {"vs_runtimes": config_dict}


class FPicBlock(Block):
    template = textwrap.dedent("""
        {% if fpic %}
        message(STATUS "Conan toolchain: Setting CMAKE_POSITION_INDEPENDENT_CODE={{ fpic }} (options.fPIC)")
        set(CMAKE_POSITION_INDEPENDENT_CODE {{ fpic }})
        {% endif %}
        """)

    def context(self):
        fpic = self._conanfile.options.get_safe("fPIC")
        if fpic is None:
            return None
        os_ = self._conanfile.settings.get_safe("os")
        if os_ and "Windows" in os_:
            self._conanfile.output.warning("Toolchain: Ignoring fPIC option defined for Windows")
            return None
        return {"fpic": "ON" if fpic else "OFF"}


class GLibCXXBlock(Block):
    template = textwrap.dedent("""
        {% if set_libcxx %}
        string(APPEND CONAN_CXX_FLAGS " {{ set_libcxx }}")
        {% endif %}
        {% if glibcxx %}
        add_definitions(-D_GLIBCXX_USE_CXX11_ABI={{ glibcxx }})
        {% endif %}
        """)

    def context(self):
        libcxx = self._conanfile.settings.get_safe("compiler.libcxx")
        if not libcxx:
            return None
        lib = libcxx_flag(self._conanfile)
        compiler = self._conanfile.settings.get_safe("compiler")
        glib = None
        if compiler in ['clang', 'apple-clang', 'gcc']:
            # we might want to remove this "1", it is the default in most distros
            if libcxx == "libstdc++":
                glib = "0"
            elif libcxx == "libstdc++11" and self._conanfile.conf.get("tools.gnu:define_libcxx11_abi"):
                glib = "1"
        return {"set_libcxx": lib, "glibcxx": glib}


class SkipRPath(Block):
    template = textwrap.dedent("""
        {% if skip_rpath %}
        set(CMAKE_SKIP_RPATH 1 CACHE BOOL "rpaths" FORCE)
        # Policy CMP0068
        # We want the old behavior, in CMake >= 3.9 CMAKE_SKIP_RPATH won't affect install_name in OSX
        set(CMAKE_INSTALL_NAME_DIR "")
        {% endif %}
        """)

    skip_rpath = False

    def context(self):
        return {"skip_rpath": self.skip_rpath}


class ArchitectureBlock(Block):
    template = textwrap.dedent("""
        string(APPEND CONAN_CXX_FLAGS " {{ arch_flag }}")
        string(APPEND CONAN_C_FLAGS " {{ arch_flag }}")
        string(APPEND CONAN_SHARED_LINKER_FLAGS " {{ arch_flag }}")
        string(APPEND CONAN_EXE_LINKER_FLAGS " {{ arch_flag }}")
        """)

    def context(self):
        arch_flag = architecture_flag(self._conanfile.settings)
        if not arch_flag:
            return
        return {"arch_flag": arch_flag}


class CppStdBlock(Block):
    template = textwrap.dedent("""
        message(STATUS "Conan toolchain: C++ Standard {{ cppstd }} with extensions {{ cppstd_extensions }}")
        set(CMAKE_CXX_STANDARD {{ cppstd }})
        set(CMAKE_CXX_EXTENSIONS {{ cppstd_extensions }})
        set(CMAKE_CXX_STANDARD_REQUIRED ON)
        """)

    def context(self):
        compiler_cppstd = self._conanfile.settings.get_safe("compiler.cppstd")
        if compiler_cppstd is None:
            return None

        if compiler_cppstd.startswith("gnu"):
            cppstd = compiler_cppstd[3:]
            cppstd_extensions = "ON"
        else:
            cppstd = compiler_cppstd
            cppstd_extensions = "OFF"
        return {"cppstd": cppstd, "cppstd_extensions": cppstd_extensions}


class SharedLibBock(Block):
    template = textwrap.dedent("""
        message(STATUS "Conan toolchain: Setting BUILD_SHARED_LIBS = {{ shared_libs }}")
        set(BUILD_SHARED_LIBS {{ shared_libs }})
        """)

    def context(self):
        try:
            shared_libs = "ON" if self._conanfile.options.shared else "OFF"
            return {"shared_libs": shared_libs}
        except ConanException:
            return None


class ParallelBlock(Block):
    template = textwrap.dedent("""
        string(APPEND CONAN_CXX_FLAGS " /MP{{ parallel }}")
        string(APPEND CONAN_C_FLAGS " /MP{{ parallel }}")
        """)

    def context(self):
        # TODO: Check this conf

        compiler = self._conanfile.settings.get_safe("compiler")
        if compiler not in ("Visual Studio", "msvc") or "Visual" not in self._toolchain.generator:
            return

        jobs = build_jobs(self._conanfile)
        if jobs:
            return {"parallel": jobs}


class AndroidSystemBlock(Block):

    template = textwrap.dedent("""
        # New toolchain things
        set(ANDROID_PLATFORM {{ ANDROID_PLATFORM }})
        set(ANDROID_STL {{ ANDROID_STL }})
        set(ANDROID_ABI {{ ANDROID_ABI }})
        include({{ ANDROID_NDK_PATH }}/build/cmake/android.toolchain.cmake)
        """)

    def context(self):
        os_ = self._conanfile.settings.get_safe("os")
        if os_ != "Android":
            return

        android_abi = {"x86": "x86",
                       "x86_64": "x86_64",
                       "armv7": "armeabi-v7a",
                       "armv8": "arm64-v8a"}.get(str(self._conanfile.settings.arch))

        # TODO: only 'c++_shared' y 'c++_static' supported?
        #  https://developer.android.com/ndk/guides/cpp-support
        libcxx_str = str(self._conanfile.settings.compiler.libcxx)

        android_ndk_path = self._conanfile.conf.get("tools.android:ndk_path")
        if not android_ndk_path:
            raise ConanException('CMakeToolchain needs tools.android:ndk_path configuration defined')
        android_ndk_path = android_ndk_path.replace("\\", "/")

        ctxt_toolchain = {
            'ANDROID_PLATFORM': self._conanfile.settings.os.api_level,
            'ANDROID_ABI': android_abi,
            'ANDROID_STL': libcxx_str,
            'ANDROID_NDK_PATH': android_ndk_path,
        }
        return ctxt_toolchain


class AppleSystemBlock(Block):
    template = textwrap.dedent("""
        {% if CMAKE_SYSTEM_NAME is defined %}
        set(CMAKE_SYSTEM_NAME {{ CMAKE_SYSTEM_NAME }})
        {% endif %}
        {% if CMAKE_SYSTEM_VERSION is defined %}
        set(CMAKE_SYSTEM_VERSION {{ CMAKE_SYSTEM_VERSION }})
        {% endif %}
        # Set the architectures for which to build.
        set(CMAKE_OSX_ARCHITECTURES {{ CMAKE_OSX_ARCHITECTURES }} CACHE STRING "" FORCE)
        # Setting CMAKE_OSX_SYSROOT SDK, when using Xcode generator the name is enough
        # but full path is necessary for others
        set(CMAKE_OSX_SYSROOT {{ CMAKE_OSX_SYSROOT }} CACHE STRING "" FORCE)
        {% if CMAKE_OSX_DEPLOYMENT_TARGET is defined %}
        # Setting CMAKE_OSX_DEPLOYMENT_TARGET if "os.version" is defined by the used conan profile
        set(CMAKE_OSX_DEPLOYMENT_TARGET "{{ CMAKE_OSX_DEPLOYMENT_TARGET }}" CACHE STRING "")
        {% endif %}
        """)

    def context(self):
        os_ = self._conanfile.settings.get_safe("os")
        if os_ not in ['Macos', 'iOS', 'watchOS', 'tvOS']:
            return None

        arch = self._conanfile.settings.get_safe("arch")
        # check valid combinations of architecture - os ?
        # for iOS a FAT library valid for simulator and device can be generated
        # if multiple archs are specified "-DCMAKE_OSX_ARCHITECTURES=armv7;armv7s;arm64;i386;x86_64"
        host_architecture = to_apple_arch(arch, default=arch)
        host_os_version = self._conanfile.settings.get_safe("os.version")
        host_sdk_name = get_apple_sdk_name(self._conanfile)

        ctxt_toolchain = {}
        if host_sdk_name:
            ctxt_toolchain["CMAKE_OSX_SYSROOT"] = host_sdk_name
        if host_architecture:
            ctxt_toolchain["CMAKE_OSX_ARCHITECTURES"] = host_architecture

        if os_ in ('iOS', "watchOS", "tvOS"):
            ctxt_toolchain["CMAKE_SYSTEM_NAME"] = os_
            ctxt_toolchain["CMAKE_SYSTEM_VERSION"] = host_os_version

        if host_os_version:
            # https://cmake.org/cmake/help/latest/variable/CMAKE_OSX_DEPLOYMENT_TARGET.html
            # Despite the OSX part in the variable name(s) they apply also to other SDKs than
            # macOS like iOS, tvOS, or watchOS.
            ctxt_toolchain["CMAKE_OSX_DEPLOYMENT_TARGET"] = host_os_version

        return ctxt_toolchain


class FindFiles(Block):
    template = textwrap.dedent("""
        {% if find_package_prefer_config %}
        set(CMAKE_FIND_PACKAGE_PREFER_CONFIG {{ find_package_prefer_config }})
        {% endif %}

        # Definition of CMAKE_MODULE_PATH
        {% if build_paths %}
        list(PREPEND CMAKE_MODULE_PATH {{ build_paths }})
        {% endif %}
        {% if generators_folder %}
        # the generators folder (where conan generates files, like this toolchain)
        list(PREPEND CMAKE_MODULE_PATH {{ generators_folder }})
        {% endif %}

        # Definition of CMAKE_PREFIX_PATH, CMAKE_XXXXX_PATH
        {% if build_paths %}
        # The explicitly defined "builddirs" of "host" context dependencies must be in PREFIX_PATH
        list(PREPEND CMAKE_PREFIX_PATH {{ build_paths }})
        {% endif %}
        {% if generators_folder %}
        # The Conan local "generators" folder, where this toolchain is saved.
        list(PREPEND CMAKE_PREFIX_PATH {{ generators_folder }} )
        {% endif %}
        {% if cmake_program_path %}
        list(PREPEND CMAKE_PROGRAM_PATH {{ cmake_program_path }})
        {% endif %}
        {% if cmake_library_path %}
        list(PREPEND CMAKE_LIBRARY_PATH {{ cmake_library_path }})
        {% endif %}
        {% if is_apple and cmake_framework_path %}
        list(PREPEND CMAKE_FRAMEWORK_PATH {{ cmake_framework_path }})
        {% endif %}
        {% if cmake_include_path %}
        list(PREPEND CMAKE_INCLUDE_PATH {{ cmake_include_path }})
        {% endif %}

        {% if cross_building %}
        if(NOT DEFINED CMAKE_FIND_ROOT_PATH_MODE_PACKAGE OR CMAKE_FIND_ROOT_PATH_MODE_PACKAGE STREQUAL "ONLY")
            set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE "BOTH")
        endif()
        if(NOT DEFINED CMAKE_FIND_ROOT_PATH_MODE_PROGRAM OR CMAKE_FIND_ROOT_PATH_MODE_PROGRAM STREQUAL "ONLY")
            set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM "BOTH")
        endif()
        if(NOT DEFINED CMAKE_FIND_ROOT_PATH_MODE_LIBRARY OR CMAKE_FIND_ROOT_PATH_MODE_LIBRARY STREQUAL "ONLY")
            set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY "BOTH")
        endif()
        {% if is_apple %}
        if(NOT DEFINED CMAKE_FIND_ROOT_PATH_MODE_FRAMEWORK OR CMAKE_FIND_ROOT_PATH_MODE_FRAMEWORK STREQUAL "ONLY")
            set(CMAKE_FIND_ROOT_PATH_MODE_FRAMEWORK "BOTH")
        endif()
        {% endif %}
        if(NOT DEFINED CMAKE_FIND_ROOT_PATH_MODE_INCLUDE OR CMAKE_FIND_ROOT_PATH_MODE_INCLUDE STREQUAL "ONLY")
            set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE "BOTH")
        endif()
        {% endif %}
    """)

    @staticmethod
    def _join_paths(paths):
        return " ".join(['"{}"'.format(p.replace('\\', '/')
                                        .replace('$', '\\$')
                                        .replace('"', '\\"')) for p in paths])

    def context(self):
        # To find the generated cmake_find_package finders
        # TODO: Change this for parameterized output location of CMakeDeps
        find_package_prefer_config = "ON"  # assume ON by default if not specified in conf
        prefer_config = self._conanfile.conf.get("tools.cmake.cmaketoolchain:find_package_prefer_config",
                                                 check_type=bool)
        if prefer_config is False:
            find_package_prefer_config = "OFF"

        os_ = self._conanfile.settings.get_safe("os")
        is_apple_ = is_apple_os(os_)

        # Read information from host context
        host_req = self._conanfile.dependencies.host.values()
        build_paths = []
        host_lib_paths = []
        host_framework_paths = []
        host_include_paths = []
        for req in host_req:
            cppinfo = req.cpp_info.aggregated_components()
            build_paths.extend(cppinfo.builddirs)
            host_lib_paths.extend(cppinfo.libdirs)
            if is_apple_:
                host_framework_paths.extend(cppinfo.frameworkdirs)
            host_include_paths.extend(cppinfo.includedirs)

        # Read information from build context
        build_req = self._conanfile.dependencies.build.values()
        build_bin_paths = []
        for req in build_req:
            cppinfo = req.cpp_info.aggregated_components()
            build_paths.extend(cppinfo.builddirs)
            build_bin_paths.extend(cppinfo.bindirs)

        return {
            "find_package_prefer_config": find_package_prefer_config,
            "generators_folder": "${CMAKE_CURRENT_LIST_DIR}",
            "build_paths": self._join_paths(build_paths),
            "cmake_program_path": self._join_paths(build_bin_paths),
            "cmake_library_path": self._join_paths(host_lib_paths),
            "cmake_framework_path": self._join_paths(host_framework_paths),
            "cmake_include_path": self._join_paths(host_include_paths),
            "is_apple": is_apple_,
            "cross_building": cross_building(self._conanfile),
        }


class UserToolchain(Block):
    template = textwrap.dedent("""
        {% for user_toolchain in paths %}
        include("{{user_toolchain}}")
        {% endfor %}
        """)

    def context(self):
        # This is global [conf] injection of extra toolchain files
        user_toolchain = self._conanfile.conf.get("tools.cmake.cmaketoolchain:user_toolchain")
        toolchains = [user_toolchain.replace("\\", "/")] if user_toolchain else []
        return {"paths": toolchains if toolchains else []}


class CMakeFlagsInitBlock(Block):
    template = textwrap.dedent("""
        if(DEFINED CONAN_CXX_FLAGS)
          string(APPEND CMAKE_CXX_FLAGS_INIT " ${CONAN_CXX_FLAGS}")
        endif()
        if(DEFINED CONAN_C_FLAGS)
          string(APPEND CMAKE_C_FLAGS_INIT " ${CONAN_C_FLAGS}")
        endif()
        if(DEFINED CONAN_SHARED_LINKER_FLAGS)
          string(APPEND CMAKE_SHARED_LINKER_FLAGS_INIT " ${CONAN_SHARED_LINKER_FLAGS}")
        endif()
        if(DEFINED CONAN_EXE_LINKER_FLAGS)
          string(APPEND CMAKE_EXE_LINKER_FLAGS_INIT " ${CONAN_EXE_LINKER_FLAGS}")
        endif()
        """)


class TryCompileBlock(Block):
    template = textwrap.dedent("""
        get_property( _CMAKE_IN_TRY_COMPILE GLOBAL PROPERTY IN_TRY_COMPILE )
        if(_CMAKE_IN_TRY_COMPILE)
            message(STATUS "Running toolchain IN_TRY_COMPILE")
            return()
        endif()
        """)


class GenericSystemBlock(Block):
    template = textwrap.dedent("""
        {% if cmake_system_name %}
        # Cross building
        set(CMAKE_SYSTEM_NAME {{ cmake_system_name }})
        {% endif %}
        {% if cmake_system_version %}
        # Cross building
        set(CMAKE_SYSTEM_VERSION {{ cmake_system_version }})
        {% endif %}
        {% if cmake_system_processor %}
        set(CMAKE_SYSTEM_PROCESSOR {{ cmake_system_processor }})
        {% endif %}

        {% if generator_platform %}
        set(CMAKE_GENERATOR_PLATFORM "{{ generator_platform }}" CACHE STRING "" FORCE)
        {% endif %}
        {% if toolset %}
        set(CMAKE_GENERATOR_TOOLSET "{{ toolset }}" CACHE STRING "" FORCE)
        {% endif %}
        {% if compiler %}
        if(NOT DEFINED ENV{CC})
        set(CMAKE_C_COMPILER {{ compiler }})
        endif()
        {% endif %}
        {% if compiler_cpp %}
        if(NOT DEFINED ENV{CXX})
        set(CMAKE_CXX_COMPILER {{ compiler_cpp }})
        endif()
        {% endif %}
        {% if compiler_rc %}
        if(NOT DEFINED ENV{RC})
        set(CMAKE_RC_COMPILER {{ compiler_rc }})
        endif()
        {% endif %}
        {% if build_type %}
        set(CMAKE_BUILD_TYPE "{{ build_type }}" CACHE STRING "Choose the type of build." FORCE)
        {% endif %}
        """)

    def _get_toolset(self, generator):
        if generator is None or ("Visual" not in generator and "Xcode" not in generator):
            return None
        settings = self._conanfile.settings
        compiler = settings.get_safe("compiler")
        if compiler == "Visual Studio":
            subs_toolset = settings.get_safe("compiler.toolset")
            if subs_toolset:
                return subs_toolset
        elif compiler == "intel-cc":
            return IntelCC(self._conanfile).ms_toolset
        elif compiler == "msvc":
            compiler_version = str(settings.compiler.version)
            compiler_update = str(settings.compiler.update)
            if compiler_update != "None":  # It is full one(19.28), not generic 19.2X
                # The equivalent of compiler 19.26 is toolset 14.26
                return "version=14.{}{}".format(compiler_version[-1], compiler_update)
            else:
                return "v14{}".format(compiler_version[-1])
        elif compiler == "clang":
            if generator and "Visual" in generator:
                if "Visual Studio 16" in generator:
                    return "ClangCL"
                else:
                    raise ConanException("CMakeToolchain compiler=clang only supported VS 16")
        return None

    def _get_generator_platform(self, generator):
        settings = self._conanfile.settings
        # Returns the generator platform to be used by CMake
        compiler = settings.get_safe("compiler")
        arch = settings.get_safe("arch")

        if settings.get_safe("os") == "WindowsCE":
            return settings.get_safe("os.platform")

        if (compiler in ("Visual Studio", "msvc")) and generator and "Visual" in generator:
            return {"x86": "Win32",
                    "x86_64": "x64",
                    "armv7": "ARM",
                    "armv8": "ARM64"}.get(arch)
        return None

    def _get_cross_build(self):
        user_toolchain = self._conanfile.conf.get("tools.cmake.cmaketoolchain:user_toolchain")
        if user_toolchain is not None:
            return None, None, None  # Will be provided by user_toolchain

        system_name = self._conanfile.conf.get("tools.cmake.cmaketoolchain:system_name")
        system_version = self._conanfile.conf.get("tools.cmake.cmaketoolchain:system_version")
        system_processor = self._conanfile.conf.get("tools.cmake.cmaketoolchain:system_processor")

        settings = self._conanfile.settings
        assert hasattr(self._conanfile, "settings_build")
        os_ = settings.get_safe("os")
        arch = settings.get_safe("arch")
        settings_build = self._conanfile.settings_build
        os_build = settings_build.get_safe("os")
        arch_build = settings_build.get_safe("arch")

        if system_name is None:  # Try to deduce
            # Handled by AppleBlock, or AndroidBlock, not here
            if os_ not in ('Macos', 'iOS', 'watchOS', 'tvOS', 'Android'):
                cmake_system_name_map = {"Neutrino": "QNX",
                                         "": "Generic",
                                         None: "Generic"}
                if os_ != os_build:
                    system_name = cmake_system_name_map.get(os_, os_)
                elif arch is not None and arch != arch_build:
                    if not ((arch_build == "x86_64") and (arch == "x86") or
                            (arch_build == "sparcv9") and (arch == "sparc") or
                            (arch_build == "ppc64") and (arch == "ppc32")):
                        system_name = cmake_system_name_map.get(os_, os_)

        if system_name is not None and system_version is None:
            system_version = settings.get_safe("os.version")

        if system_name is not None and system_processor is None:
            if arch != arch_build:
                system_processor = arch

        return system_name, system_version, system_processor

    def _get_compiler(self, generator):
        compiler = self._conanfile.settings.get_safe("compiler")
        os_ = self._conanfile.settings.get_safe("os")

        compiler_c = compiler_cpp = compiler_rc = None

        # TODO: Check if really necessary now that conanvcvars is used
        if "Ninja" in str(generator) and is_msvc(self._conanfile):
            compiler_c = compiler_cpp = "cl"
        elif os_ == "Windows" and compiler == "clang" and "Visual" not in str(generator):
            compiler_rc = "clang"
            compiler_c = "clang"
            compiler_cpp = "clang++"

        return compiler_c, compiler_cpp, compiler_rc

    def context(self):
        # build_type (Release, Debug, etc) is only defined for single-config generators
        generator = self._toolchain.generator
        generator_platform = self._get_generator_platform(generator)
        toolset = self._get_toolset(generator)

        compiler, compiler_cpp, compiler_rc = self._get_compiler(generator)

        build_type = self._conanfile.settings.get_safe("build_type")
        build_type = build_type if not is_multi_configuration(generator) else None

        system_name, system_version, system_processor = self._get_cross_build()

        return {"compiler": compiler,
                "compiler_rc": compiler_rc,
                "compiler_cpp": compiler_cpp,
                "toolset": toolset,
                "generator_platform": generator_platform,
                "build_type": build_type,
                "cmake_system_name": system_name,
                "cmake_system_version": system_version,
                "cmake_system_processor": system_processor}


class ToolchainBlocks:
    def __init__(self, conanfile, toolchain, items=None):
        self._blocks = OrderedDict()
        self._conanfile = conanfile
        self._toolchain = toolchain
        if items:
            for name, block in items:
                self._blocks[name] = block(conanfile, toolchain)

    def remove(self, name):
        del self._blocks[name]

    def __setitem__(self, name, block_type):
        # Create a new class inheriting Block with the elements of the provided one
        block_type = type('proxyUserBlock', (Block,), dict(block_type.__dict__))
        self._blocks[name] = block_type(self._conanfile, self._toolchain)

    def __getitem__(self, name):
        return self._blocks[name]

    def process_blocks(self):
        result = []
        for b in self._blocks.values():
            content = b.get_rendered_content()
            if content:
                result.append(content)
        return result

