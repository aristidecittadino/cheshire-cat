import glob
import json
import importlib
import time
import shutil
import os
from inspect import getmembers, isfunction  # , signature
from typing import Dict

from cat.log import log
from cat.utils import to_camel_case
from cat.mad_hatter.decorators import CatTool, CatHooks
from cat.infrastructure.package import Package


# This class is responsible for plugins functionality:
# - loading
# - prioritizing
# - executing
class MadHatter:
    # loads and execute plugins
    # - enter into the plugin folder and loads everthing
    #   that is decorated or named properly
    # - orders plugged in hooks by name and priority
    # - exposes functionality to the cat

    def __init__(self, ccat):
        self.ccat = ccat
        self.find_plugins()

    def install_plugin(self, package_plugin):

        # extract zip/tar file into plugin folder
        plugin_folder = self.ccat.get_plugin_path()
        pkg_obj = Package(package_plugin)
        pkg_obj.unpackage(plugin_folder)
        
        # re-discover and reorder hooks
        # TODO: this can be optimized by only discovering the new plugin
        #   and having a method to re-sort hooks
        self.find_plugins()
        # keep tools in sync (embed new tools)
        self.embed_tools()

    def uninstall_plugin(self, plugin_id):

        # remove plugin folder
        shutil.rmtree(self.ccat.get_plugin_path() + plugin_id)

        # re-discover and reorder hooks
        # TODO: this can be optimized by only discovering the new plugin
        #   and having a method to re-sort hooks
        self.find_plugins()
        # keep tools in sync (embed new tools)
        self.embed_tools()

    def find_plugin(self, folder):

        # search for .py
        py_files_path = os.path.join(folder, "**/*.py")
        py_files = glob.glob(py_files_path, recursive=True)

        plugin_info = None
        plugin_tools = []

        # in order to consider it a plugin makes sure there are py files
        #   inside the plugin directory
        if len(py_files) > 0:
            plugin_info = self.get_plugin_metadata(folder)

            for py_file in py_files:
                plugin_name = py_file.replace("/", ".").replace(".py", "")  # this is UGLY I know. I'm sorry

                plugin_module = importlib.import_module(plugin_name)
                plugin_tools += getmembers(plugin_module, self.is_cat_tool)


        return plugin_info, plugin_tools

    # find all functions in plugin folder decorated with @hook or @tool
    def find_plugins(self):
        # plugins are found in the plugins folder,
        #   plus the default core plugin
        #   (where default hooks and tools are defined)
        core_folder = "cat/mad_hatter/core_plugin/"
        plugin_folders = [core_folder] + glob.glob("cat/plugins/*/")
        # TODO: use cat.get_plugin_path() so it can be mocked from tests

        all_plugins = []
        all_tools = []

        for folder in plugin_folders:
            plugin_info, plugin_tools = self.find_plugin(folder)
            if plugin_info:
                all_plugins.append(plugin_info)
            if plugin_tools:
                all_tools += plugin_tools

        log("Plugins loading:", "INFO")
        for plugin in all_plugins:
            log("> " + plugin["name"], "DEBUG")

        log("Hooks loading", "INFO")
        all_hooks = CatHooks.sort_hooks()
        for hook in all_hooks:
            log("> " + hook["hook_name"])

        log("Tools loading")
        all_tools_fixed = []
        for t in all_tools:
            t_fix = t[1]  # it was a tuple, the Tool is the second element

            # Prepare the tool to be used in the Cat (setting the cat instanca, adding properties)
            t_fix.augment_tool(self.ccat)

            all_tools_fixed.append(t_fix)
        log(all_tools_fixed, "INFO")

        self.hooks, self.tools, self.plugins = all_hooks, all_tools_fixed, all_plugins
    
    # check if plugin exists
    def plugin_exists(self, plugin_id):

        # there should be only one plugin with that id
        found = [plugin for plugin in self.plugins if plugin["id"] == plugin_id]
        return len(found) > 0


    # loops over tools and assign an embedding each. If an embedding is not present in vectorDB, it is created and saved
    def embed_tools(self):

        # retrieve from vectorDB all tool embeddings
        all_tools_points = self.ccat.memory.vectors.procedural.get_all_points()

        # easy access to plugin tools
        plugins_tools_index = {t.description: t for t in self.tools}

        points_to_be_deleted = []
        
        vector_db = self.ccat.memory.vectors.vector_db

        # loop over vectors
        for record in all_tools_points:
            # if the tools is active in plugins, assign embedding
            try:
                tool_description = record.payload["page_content"]
                plugins_tools_index[tool_description].embedding = record.vector
                # log(plugins_tools_index[tool_description], "WARNING")
            # else delete it
            except Exception as e:
                log(f"Deleting embedded tool: {record.payload['page_content']}", "WARNING")
                points_to_be_deleted.append(record.id)

        if len(points_to_be_deleted) > 0:
            vector_db.delete(
                collection_name="procedural",
                points_selector=points_to_be_deleted
            )

        # loop over tools
        for tool in self.tools:
            # if there is no embedding, create it
            if not tool.embedding:
                # save it to DB
                ids_inserted = self.ccat.memory.vectors.procedural.add_texts(
                    [tool.description],
                    [{
                        "source": "tool",
                        "when": time.time(),
                        "name": tool.name,
                        "docstring": tool.docstring
                    }],
                )

                # retrieve saved point and assign embedding to the Tool
                records_inserted = vector_db.retrieve(
                    collection_name="procedural",
                    ids=ids_inserted,
                    with_vectors=True
                )
                tool.embedding = records_inserted[0].vector

                log(f"Newly embedded tool: {tool.description}", "WARNING")

    # Tries to load the plugin metadata from the provided plugin folder
    def get_plugin_metadata(self, plugin_folder: str):
        plugin_id = os.path.basename(os.path.normpath(plugin_folder))
        plugin_json_metadata_file_name = "plugin.json"
        plugin_json_metadata_file_path = os.path.join(plugin_folder, plugin_json_metadata_file_name)
        meta = {"id": plugin_id}
        json_file_data = {}

        if os.path.isfile(plugin_json_metadata_file_path):
            try:
                json_file = open(plugin_json_metadata_file_path)
                json_file_data = json.load(json_file)
                json_file.close()
            except Exception:
                log(f"Loading plugin {plugin_folder} metadata, defaulting to generated values", "INFO")

        meta["name"] = json_file_data.get("name", to_camel_case(plugin_id))
        meta["description"] = json_file_data.get("description", (
            "Description not found for this plugin. "
            f"Please create a `{plugin_json_metadata_file_name}`"
            " in the plugin folder."
        ))
        meta["author_name"] = json_file_data.get("author_name", "Unknown author")
        meta["author_url"] = json_file_data.get("author_url", "")
        meta["plugin_url"] = json_file_data.get("plugin_url", "")
        meta["tags"] = json_file_data.get("tags", "unknown")
        meta["thumb"] = json_file_data.get("thumb", "")
        meta["version"] = json_file_data.get("version", "0.0.1")

        return meta
    
    # Tries to get the plugin settings from the provided plugin id
    def get_plugin_settings(self, plugin_id: str):
        settings_file_path = os.path.join("cat/plugins", plugin_id, "settings.json")
        settings = { "active": False }

        if os.path.isfile(settings_file_path):
            try:
                json_file = open(settings_file_path)
                settings = json.load(json_file)
                if "active" not in settings:
                    settings["active"] = False
                json_file.close()
            except Exception:
                log(f"Loading plugin {plugin_id} settings, defaulting to -> 'active': False", "INFO")
    
        return settings
    
    # Tries to save the plugin settings of the provided plugin id
    def save_plugin_settings(self, plugin_id: str, settings: Dict):
        settings_file_path = os.path.join("cat/plugins", plugin_id, "settings.json")
        updated_settings = settings

        try:
            json_file = open(settings_file_path, 'r+')
            current_settings = json.load(json_file)
            json_file.close()
            updated_settings = { **current_settings, **settings }
            json_file = open(settings_file_path, 'w')
            json.dump(updated_settings, json_file, indent=4)
            json_file.close()
        except Exception:
            log(f"Unable to save plugin {plugin_id} settings", "INFO")
    
        return updated_settings

    # a plugin function has to be decorated with @hook
    # (which returns a function named "cat_function_wrapper")
    def is_cat_hook(self, obj):
        return isfunction(obj) and obj.__name__ == "cat_hook_wrapper"

    # a plugin tool function has to be decorated with @tool
    # (which returns an instance of langchain.agents.Tool)
    def is_cat_tool(self, obj):
        return isinstance(obj, CatTool)

    # execute requested hook
    def execute_hook(self, hook_name, *args):
        for h in self.hooks:
            if hook_name == h["hook_name"]:
                hook = h["hook_function"]
                return hook(*args, cat=self.ccat)

        # every hook must have a default in core_plugin
        raise Exception(f"Hook {hook_name} not present in any plugin")
