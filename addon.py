import bpy
import mathutils
import json
import threading
import socket
import time
import traceback
import io
from contextlib import redirect_stdout
from bpy.props import IntProperty, BoolProperty

bl_info = {
    "name": "BOB",
    "author": "Rishabh Verma",
    "version": (1, 2),
    "blender": (3, 0, 0),
    "location": "Top Bar > BOB",
    "description": "Connect Blender to Agent",
    "category": "Interface",
}


class BlenderServer:
    def __init__(self, host='localhost', port=12345):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    def start(self):
        if self.running:
            return

        self.running = True

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            print(f"Blender server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {e}")
            self.stop()

    def stop(self):
        self.running = False

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server_thread = None

        print("Blender server stopped")

    def _server_loop(self):
        self.socket.settimeout(1.0)

        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {e}")
                    time.sleep(0.5)
            except Exception as e:
                if not self.running:
                    break
                print(f"Error in server loop: {e}")
                time.sleep(0.5)

    def _handle_client(self, client):
        client.settimeout(None)
        buffer = b''

        try:
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        break

                    buffer += data
                    try:
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        def execute_wrapper():
                            try:
                                response = self.execute_command(command)
                                client.sendall(json.dumps(response).encode('utf-8'))
                            except Exception as e:
                                traceback.print_exc()
                                try:
                                    client.sendall(json.dumps({
                                        "status": "error",
                                        "message": str(e)
                                    }).encode('utf-8'))
                                except Exception:
                                    pass
                            return None

                        bpy.app.timers.register(execute_wrapper, first_interval=0.0)
                    except json.JSONDecodeError:
                        pass
                except Exception as e:
                    print(f"Error receiving data: {e}")
                    break
        except Exception as e:
            print(f"Error in client handler: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def execute_command(self, command):
        cmd_type = command.get("type")
        params = command.get("params", {})

        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "execute_code": self.execute_code,
            "set_camera": self.set_camera,
            "get_render_preview": self.get_render_preview,
        }

        handler = handlers.get(cmd_type)
        if not handler:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        try:
            return {"status": "success", "result": handler(**params)}
        except Exception as e:
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def get_scene_info(self):
        scene_info = {
            "name": bpy.context.scene.name,
            "object_count": len(bpy.context.scene.objects),
            "materials_count": len(bpy.data.materials),
            "objects": [],
        }

        for i, obj in enumerate(bpy.context.scene.objects):
            if i >= 10:
                break
            scene_info["objects"].append({
                "name": obj.name,
                "type": obj.type,
                "location": [round(float(v), 2) for v in obj.location],
            })

        return scene_info

    @staticmethod
    def _get_aabb(obj):
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        local_corners = [mathutils.Vector(c) for c in obj.bound_box]
        world_corners = [obj.matrix_world @ c for c in local_corners]

        min_corner = mathutils.Vector(map(min, zip(*world_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_corners)))

        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name):
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": list(obj.location),
            "rotation": list(obj.rotation_euler),
            "scale": list(obj.scale),
            "visible": obj.visible_get(),
            "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        }

        if obj.type == 'MESH':
            obj_info["world_bounding_box"] = self._get_aabb(obj)
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def execute_code(self, code):
        namespace = {"bpy": bpy}
        capture_buffer = io.StringIO()
        with redirect_stdout(capture_buffer):
            exec(code, namespace, namespace)
        return {"executed": True, "result": capture_buffer.getvalue()}

    def set_camera(self, location=None, rotation_degrees=None, camera_name="Camera",
                   focal_length=None, set_active=True):
        import math
        cam_obj = bpy.data.objects.get(camera_name)
        if not cam_obj:
            cam_data = bpy.data.cameras.new(name=camera_name)
            cam_obj = bpy.data.objects.new(camera_name, cam_data)
            bpy.context.scene.collection.objects.link(cam_obj)

        if location is not None:
            cam_obj.location = location
        if rotation_degrees is not None:
            cam_obj.rotation_euler = [math.radians(r) for r in rotation_degrees]
        if focal_length is not None:
            cam_obj.data.lens = focal_length
        if set_active:
            bpy.context.scene.camera = cam_obj

        return {
            "name": cam_obj.name,
            "location": [round(float(v), 4) for v in cam_obj.location],
            "rotation_degrees": [round(math.degrees(r), 2) for r in cam_obj.rotation_euler],
            "focal_length_mm": round(cam_obj.data.lens, 2),
            "is_active_camera": bpy.context.scene.camera == cam_obj,
        }

    def get_render_preview(self, filepath=None, max_size=512):
        if not filepath:
            return {"error": "No filepath provided"}

        scene = bpy.context.scene
        orig_x          = scene.render.resolution_x
        orig_y          = scene.render.resolution_y
        orig_pct        = scene.render.resolution_percentage
        orig_filepath   = scene.render.filepath
        orig_fmt        = scene.render.image_settings.file_format
        orig_engine     = scene.render.engine

        try:
            for eevee_engine in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):
                try:
                    scene.render.engine = eevee_engine
                    break
                except TypeError:
                    pass
            scene.render.resolution_x          = max_size
            scene.render.resolution_y          = max_size
            scene.render.resolution_percentage = 100
            scene.render.filepath              = filepath
            scene.render.image_settings.file_format = 'PNG'

            bpy.ops.render.render(write_still=True)

            return {
                "success": True,
                "filepath": filepath,
                "resolution": [max_size, max_size],
                "engine": scene.render.engine,
            }
        finally:
            scene.render.engine                = orig_engine
            scene.render.resolution_x          = orig_x
            scene.render.resolution_y          = orig_y
            scene.render.resolution_percentage = orig_pct
            scene.render.filepath              = orig_filepath
            scene.render.image_settings.file_format = orig_fmt


class TOPBAR_MT_BOB(bpy.types.Menu):
    bl_label = "BOB"
    bl_idname = "TOPBAR_MT_BOB"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "blendermcp_port")
        layout.separator()

        if not scene.blendermcp_server_running:
            layout.operator("blendermcp.start_server", text="Connect", icon='PLAY')
        else:
            layout.operator("blendermcp.stop_server", text="Disconnect", icon='PAUSE')
            layout.label(text=f"Running on port {scene.blendermcp_port}", icon='INFO')


def draw_bob_menu(self, context):
    self.layout.menu("TOPBAR_MT_BOB")


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Start Server"
    bl_description = "Start the Blender server"

    def execute(self, context):
        scene = context.scene

        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderServer(port=scene.blendermcp_port)

        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = True

        return {'FINISHED'}


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop Server"
    bl_description = "Stop the Blender server"

    def execute(self, context):
        scene = context.scene

        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {'FINISHED'}


def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=12345,
        min=1024,
        max=65535,
    )
    bpy.types.Scene.blendermcp_server_running = BoolProperty(
        name="Server Running",
        default=False,
    )

    bpy.utils.register_class(TOPBAR_MT_BOB)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)
    bpy.types.TOPBAR_MT_editor_menus.append(draw_bob_menu)


def unregister():
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    bpy.types.TOPBAR_MT_editor_menus.remove(draw_bob_menu)
    bpy.utils.unregister_class(TOPBAR_MT_BOB)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running


if __name__ == "__main__":
    register()
