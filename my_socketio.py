from rasa.core.channels.socketio import SocketIOInput
from sanic import Blueprint
import socketio

class PatchedSocketIOInput(SocketIOInput):
    def blueprint(self, on_new_message):
        sio = socketio.AsyncServer(async_mode="sanic", cors_allowed_origins="*")
        app = Blueprint("socketio_webhook", __name__)
        sio.attach(app)  # ✅ 이게 핵심

        @sio.on("connect", namespace=self.namespace)
        async def connect(sid, environ, auth=None):
            print(f"🔌 연결됨: {sid}, auth: {auth}")

        @sio.on(self.user_message_evt, namespace=self.namespace)
        async def handle_message(sid, data):
            message = data.get("message")
            metadata = data.get("metadata")
            await on_new_message(
                self._message(
                    message,
                    self.get_sender_id(sid),
                    input_channel=self.name(),
                    metadata=metadata,
                )
            )

        return app  # ✅ Sanic Blueprint 반환
