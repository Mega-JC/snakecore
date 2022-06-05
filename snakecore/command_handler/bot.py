"""This file is a part of the source code for snakecore.
This project has been licensed under the MIT license.
Copyright (c) 2022-present pygame-community

This file defines a customized drop-in replacement for `discord.ext.commands.Bot`
and `discord.ext.commands.AutoShardedBot` with more features.
"""

import importlib
import inspect
import sys
import types
from typing import Any, Dict, Optional
from discord.ext import commands
from discord.ext.commands import errors

__all__ = (
    "Bot",
    "AutoShardedBot",
)


def _is_submodule(parent: str, child: str) -> bool:
    return parent == child or child.startswith(parent + ".")


class ExtBotBase(commands.bot.BotBase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # necessary evil to get extensions dict
        self.__extensions: Dict[str, types.ModuleType] = self._BotBase__extensions

    async def _call_extension_function(self, function, options: Dict[str, Any]):
        sig = None
        try:
            sig = inspect.signature(function)
        except (ValueError, TypeError):
            pass

        if (
            sig is not None
            and "options" in sig.parameters
            and sig.parameters["options"].kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ):
            await function(self, options=options)
        else:
            await function(self)

    async def _load_from_module_spec(
        self,
        spec: importlib.machinery.ModuleSpec,
        key: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:

        if not isinstance(options, dict):
            return await super()._load_from_module_spec(spec, key)

        # precondition: key not in self.__extensions
        lib = importlib.util.module_from_spec(spec)
        sys.modules[key] = lib
        try:
            spec.loader.exec_module(lib)  # type: ignore
        except Exception as e:
            del sys.modules[key]
            raise errors.ExtensionFailed(key, e) from e

        try:
            setup = getattr(lib, "setup")
        except AttributeError:
            del sys.modules[key]
            raise errors.NoEntryPointError(key)

        try:
            await self._call_extension_function(setup, options)
        except Exception as e:
            del sys.modules[key]
            await self._remove_module_references(lib.__name__)
            await self._call_module_finalizers(lib, key)
            raise errors.ExtensionFailed(key, e) from e
        else:
            self.__extensions[key] = lib

    async def load_extension(
        self,
        name: str,
        *,
        package: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Loads an extension.

        An extension is a python module that contains commands, cogs, or listeners.

        An extension must have a global function, setup defined as the entry point on what to do when the extension is loaded.

        This method adds an extra keyword parameter to `Bot.load_extension` that can
        be used to pass options to the `setup` function of the extension.

        Args:
            name (str): The extension name to reload. It must be dot separated like
              regular Python imports if accessing a sub-module. e.g.
              `foo.test` if you want to import `foo/test.py`.
            package (Optional[str], optional): The package name to resolve relative imports with.
              This is required when reloading an extension using a relative path, e.g `.foo.test`.
              Defaults to None.
            options (Optional[Dict[str, Any]], optional): A dictionary of 'options' to
              be passed to the `setup` function of the specified extension as a keyword
              argument. If a dictionary is specified, the `setup` function will be checked for a
              keyword parameter of the same name before it can recieve the dictionary. If no
              matching parameter is found or the specified object is not a dictionary,
              the keyword argument will be omitted. Defaults to None.

        Raises:
            ExtensionNotFound: The extension could not be imported.
              This is also raised if the name of the extension could not
              be resolved using the provided `package` parameter.
            ExtensionAlreadyLoaded: The extension is already loaded.
            NoEntryPointError: The extension does not have a setup function.
            ExtensionFailed: The extension or its setup function had an execution error.
        """

        if not isinstance(options, dict):
            return await super().load_extension(name, package=package)

        name = self._resolve_name(name, package)
        if name in self.__extensions:
            raise commands.errors.ExtensionAlreadyLoaded(name)

        spec = importlib.util.find_spec(name)
        if spec is None:
            raise commands.errors.ExtensionNotFound(name)

        await self._load_from_module_spec(spec, name, options)

    async def reload_extension(
        self,
        name: str,
        *,
        package: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically reloads an extension.

        This replaces the extension with the same extension, only refreshed. This is
        equivalent to a `unload_extension` followed by a `load_extension`
        except done in an atomic way. That is, if an operation fails mid-reload then
        the bot will roll-back to the prior working state.

        Args:
            name (str): The extension name to reload. It must be dot separated like
              regular Python imports if accessing a sub-module. e.g.
              `foo.test` if you want to import `foo/test.py`.
            package (Optional[str], optional): The package name to resolve relative imports with.
              This is required when reloading an extension using a relative path, e.g `.foo.test`.
              Defaults to None.
            options (Optional[Dict[str, Any]], optional): A dictionary of 'options' to
              be passed to the `setup` function of the specified extension as a keyword
              argument. If a dictionary is specified, the `setup` function will be checked for a
              keyword parameter of the same name before it can recieve the dictionary. If no
              matching parameter is found or the specified object is not a dictionary,
              the keyword argument will be omitted. Defaults to None.

        Raises:
            ExtensionNotLoaded: The extension was not loaded.
            ExtensionNotFound: The extension could not be imported.
              This is also raised if the name of the extension could not
              be resolved using the provided ``package`` parameter.
            NoEntryPointError: The extension does not have a setup function.
            ExtensionFailed: The extension setup function had an execution error.
        """

        if not isinstance(options, dict):
            return await super().reload_extension(name, package=package)

        name = self._resolve_name(name, package)
        lib = self.__extensions.get(name)
        if lib is None:
            raise commands.errors.ExtensionNotLoaded(name)

        # get the previous module states from sys modules
        # fmt: off
        modules = {
            name: module
            for name, module in sys.modules.items()
            if _is_submodule(lib.__name__, name)
        }
        # fmt: on

        try:
            # Unload and then load the module...
            await self._remove_module_references(lib.__name__)
            await self._call_module_finalizers(lib, name)
            await self.load_extension(name, options=options)
        except Exception:
            # if the load failed, the remnants should have been
            # cleaned from the load_extension function call
            # so let's load it from our old compiled library.

            await self._call_extension_function(lib.setup, options)

            self.__extensions[name] = lib

            # revert sys.modules back to normal and raise back to caller
            sys.modules.update(modules)
            raise

    async def _call_module_finalizers(
        self, lib: types.ModuleType, key: str, options: Optional[Dict[str, Any]] = None
    ) -> None:

        if not isinstance(options, dict):
            return await super()._call_module_finalizers(lib, key)

        try:
            func = getattr(lib, "teardown")
        except AttributeError:
            pass
        else:
            try:
                await self._call_extension_function(func, options)
            except Exception:
                pass
        finally:
            self.__extensions.pop(key, None)
            sys.modules.pop(key, None)
            name = lib.__name__
            for module in list(sys.modules.keys()):
                if _is_submodule(name, module):
                    del sys.modules[module]

    async def unload_extension(
        self,
        name: str,
        *,
        package: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Unloads an extension.

        When the extension is unloaded, all commands, listeners, and cogs are
        removed from the bot and the module is un-imported.

        The extension can provide an optional global function, ``teardown``,
        to do miscellaneous clean-up if necessary. This function takes a single
        parameter, the ``bot``, similar to ``setup`` from
        :meth:`~.Bot.load_extension`.

        name (str): The extension name to unload. It must be dot separated like
            regular Python imports if accessing a sub-module. e.g.
            `foo.test` if you want to import `foo/test.py`.
        package (Optional[str], optional): The package name to resolve relative imports with.
            This is required when reloading an extension using a relative path, e.g `.foo.test`.
            Defaults to None.
        options (Optional[Dict[str, Any]], optional): A dictionary of 'options' to
            be passed to the `teardown` function of the specified extension as a keyword
            argument. If a dictionary is specified, the `teardown` function will be checked for a
            keyword parameter of the same name before it can recieve the dictionary. If no
            matching parameter is found or the specified object is not a dictionary,
            the keyword argument will be omitted. Defaults to None.

        Raises:
            ExtensionNotLoaded: The extension was not loaded.
            ExtensionNotFound: The extension could not be imported.
              This is also raised if the name of the extension could not
              be resolved using the provided ``package`` parameter.
        """
        if not isinstance(options, dict):
            return await super().unload_extension(name, package=package)

        name = self._resolve_name(name, package)
        lib = self.__extensions.get(name)
        if lib is None:
            raise errors.ExtensionNotLoaded(name)

        await self._remove_module_references(lib.__name__)
        await self._call_module_finalizers(lib, name, options)


class ExtBot(commands.Bot, ExtBotBase):
    """A drop-in replacement for `discord.ext.commands.Bot` with more extension-loading features."""

    pass


class ExtAutoShardedBot(commands.AutoShardedBot, ExtBotBase):
    """A drop-in replacement for `discord.ext.commands.AutoShardedBot` with more extension-loading features."""

    pass


Bot = ExtBot  # export with familiar name
"""A drop-in replacement for `discord.ext.commands.Bot` with more features."""
AutoShardedBot = ExtAutoShardedBot
"""A drop-in replacement for `discord.ext.commands.AutoShardedBot` with more features."""
