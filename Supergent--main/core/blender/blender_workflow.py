"""
WISE Blender Workflow Subsystem.
Provides structured, deterministic integration with the Blender 5.2 MCP Daemon (127.0.0.1:9876).
Enforces semantic 3D construction via structured tools rather than blind mouse clicks.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("WISE.Blender.Workflow")


class BlenderWorkflowManager:
    """
    Governs structured 3D operations via the Blender MCP socket bridge.
    Executes scene creation, transforms, materials, lighting, cameras,
    rendering, and .blend checkpoint exports with physical artifact verification.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9876) -> None:
        self.host = host
        self.port = port

    def is_available(self) -> bool:
        """Checks if the Blender MCP socket daemon is actively listening."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.5)
                return s.connect_ex((self.host, self.port)) == 0
        except Exception:
            return False

    def execute_command(
        self,
        category: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Sends a structured command to the Blender MCP socket daemon."""
        req = {
            "id": f"wise-{int(time.time() * 1000)}",
            "type": "command",
            "category": category,
            "action": action,
            "params": params or {},
        }
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((self.host, self.port))
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))

                # Read response until newline
                buf = ""
                while True:
                    chunk = s.recv(65536).decode("utf-8")
                    if not chunk:
                        break
                    buf += chunk
                    if "\n" in buf:
                        break

                if not buf.strip():
                    return {"success": False, "error": "Empty response from Blender socket"}
                return json.loads(buf.strip())
        except Exception as e:
            LOG.error("Blender MCP socket communication failed: %s", e)
            return {"success": False, "error": str(e)}

    def list_objects(self) -> List[Dict[str, Any]]:
        """Queries all objects in the active Blender scene."""
        res = self.execute_command("object", "list", {"limit": 200})
        if res.get("success"):
            return res.get("data", {}).get("objects", [])
        return []

    def create_primitive(
        self,
        obj_type: str,
        name: Optional[str] = None,
        location: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        rotation: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> Dict[str, Any]:
        """Creates a mesh primitive object in the scene."""
        params = {
            "type": obj_type.upper(),
            "name": name,
            "location": list(location),
            "rotation": list(rotation),
            "scale": list(scale),
        }
        return self.execute_command("object", "create", params)

    def create_material(
        self,
        name: str,
        color: List[float],
        metallic: float = 0.0,
        roughness: float = 0.5,
    ) -> Dict[str, Any]:
        """Creates a PBR material node."""
        params = {
            "name": name,
            "color": color,
            "metallic": metallic,
            "roughness": roughness,
        }
        return self.execute_command("material", "create", params)

    def assign_material(self, material_name: str, object_name: str) -> Dict[str, Any]:
        """Assigns an existing material to an object."""
        return self.execute_command("material", "assign", {
            "material_name": material_name,
            "object_name": object_name,
        })

    def create_light(
        self,
        name: str,
        light_type: str = "POINT",
        location: Tuple[float, float, float] = (0.0, 0.0, 5.0),
        energy: float = 100.0,
    ) -> Dict[str, Any]:
        """Creates a light source in the scene."""
        params = {
            "type": light_type.upper(),
            "name": name,
            "location": list(location),
            "energy": energy,
            "power": energy,
        }
        return self.execute_command("lighting", "create", params)

    def render_image(
        self,
        output_path: str,
        frame: Optional[int] = None,
        camera: Optional[str] = None,
        write_still: bool = True,
    ) -> Dict[str, Any]:
        """Triggers a still frame render and writes image to disk."""
        params = {
            "output_path": output_path,
            "frame": frame,
            "camera": camera,
            "write_still": write_still,
        }
        return self.execute_command("render", "image", params, timeout=30.0)

    def save_checkpoint(self, name: str, description: str = "") -> Dict[str, Any]:
        """Saves a named .blend project file checkpoint."""
        return self.execute_command("checkpoint", "save", {
            "name": name,
            "description": description,
        })

    def build_and_verify_scene(
        self,
        scene_name: str,
        render_output_path: str,
    ) -> Dict[str, Any]:
        """
        End-to-End High-Level Workflow:
        Creates a complete multi-object scene with materials, lighting,
        renders a preview image, and saves the .blend file artifact.
        Verifies both physical artifacts and scene object population.
        """
        if not self.is_available():
            return {
                "success": False,
                "error": "Blender MCP daemon is not running on 127.0.0.1:9876",
                "status": "CONNECTED_BUT_ENVIRONMENT_BLOCKED",
            }

        # 1. Create Ground Plane
        self.create_primitive("PLANE", name=f"{scene_name}_Ground", location=(0, 0, 0), scale=(5, 5, 1))

        # 2. Create Centerpiece Sphere
        self.create_primitive("SPHERE", name=f"{scene_name}_Sphere", location=(0, 0, 1.2), scale=(1.2, 1.2, 1.2))

        # 3. Create Supporting Cube
        self.create_primitive("CUBE", name=f"{scene_name}_Cube", location=(2.5, 0, 0.8), scale=(0.8, 0.8, 0.8))

        # 4. Create and Assign Materials
        gold_mat = f"{scene_name}_Gold"
        self.create_material(gold_mat, color=[1.0, 0.75, 0.1, 1.0], metallic=0.9, roughness=0.15)
        self.assign_material(gold_mat, f"{scene_name}_Sphere")

        blue_mat = f"{scene_name}_Blue"
        self.create_material(blue_mat, color=[0.1, 0.3, 0.9, 1.0], metallic=0.2, roughness=0.4)
        self.assign_material(blue_mat, f"{scene_name}_Cube")

        # 5. Add Key Light
        self.create_light(f"{scene_name}_KeyLight", light_type="SUN", location=(4, -4, 8), energy=4.0)

        # 6. Render preview image
        render_res = self.render_image(render_output_path, write_still=True)
        img_ok = False
        img_size = 0
        if os.path.isfile(render_output_path):
            img_size = os.path.getsize(render_output_path)
            img_ok = (img_size > 1000)

        # 7. Save .blend checkpoint
        save_res = self.save_checkpoint(scene_name, description=f"Automated scene for {scene_name}")
        blend_path = save_res.get("data", {}).get("path", "")
        blend_ok = False
        blend_size = 0
        if blend_path and os.path.isfile(blend_path):
            blend_size = os.path.getsize(blend_path)
            blend_ok = (blend_size > 1000)

        # 8. Query scene objects
        objects = self.list_objects()
        obj_names = [o.get("name") for o in objects]

        success = img_ok and blend_ok and (f"{scene_name}_Sphere" in obj_names)

        return {
            "success": success,
            "scene_name": scene_name,
            "objects_count": len(objects),
            "objects": obj_names,
            "render_image_path": render_output_path,
            "render_image_size": img_size,
            "render_verified": img_ok,
            "blend_file_path": blend_path,
            "blend_file_size": blend_size,
            "blend_verified": blend_ok,
        }


_GLOBAL_BLENDER_WORKFLOW: Optional[BlenderWorkflowManager] = None


def get_blender_workflow() -> BlenderWorkflowManager:
    global _GLOBAL_BLENDER_WORKFLOW
    if _GLOBAL_BLENDER_WORKFLOW is None:
        _GLOBAL_BLENDER_WORKFLOW = BlenderWorkflowManager()
    return _GLOBAL_BLENDER_WORKFLOW
