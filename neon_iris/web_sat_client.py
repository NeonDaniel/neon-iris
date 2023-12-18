"""Runs a web server that serves the Neon AI Web UI and Voice Satellite."""
import json
from os import makedirs
from os.path import isdir, join
from threading import Event
from time import time
from typing import Dict, List, Optional
from uuid import uuid4

import numpy as np
import resampy
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from neon_utils.file_utils import decode_base64_string_to_file
from openwakeword import Model
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_utils import LOG
from ovos_utils.xdg_utils import xdg_data_home

from neon_iris.client import NeonAIClient
from neon_iris.models.web_sat import UserInput, UserInputResponse


class WebSatNeonClient(NeonAIClient):
    """Neon AI Web UI and Voice Satellite client."""

    def __init__(self, lang: str = None):
        config = Configuration()
        self.config = config.get("iris") or dict()
        self.mq_config = config.get("MQ")
        if not self.mq_config:
            raise ValueError(
                "Missing MQ configuration, please set it in ~/.config/neon/neon.yaml"
            )
        NeonAIClient.__init__(self, self.mq_config)
        self.router = APIRouter()
        self._await_response = Event()
        self._response = None
        self._transcribed = None
        self._current_tts = dict()
        self._profiles: Dict[str, dict] = dict()
        self._audio_path = join(
            xdg_data_home(), "iris", "stt"
        )  # TODO: Clear periodically, or have persistent storage
        if not isdir(self._audio_path):
            makedirs(self._audio_path)
        self.default_lang = lang or self.config.get("default_lang")
        LOG.name = "iris"
        LOG.init(self.config.get("logs"))
        # OpenWW
        self.oww_model = Model(inference_framework="tflite")
        # FastAPI
        self.templates = Jinja2Templates(directory="neon_iris/static/templates")
        self.build_routes()

    def get_lang(self, session_id: str):
        """Get the language for a session."""
        if session_id and session_id in self._profiles:
            return self._profiles[session_id]["speech"]["stt_language"]
        return self.user_config["speech"]["stt_language"] or self.default_lang

    def handle_api_response(self, message: Message):
        """
        Catch-all handler for `.response` messages routed to this client that
        are not explicitly handled (i.e. get_stt, get_tts)
        @param message: Response message to something emitted by this client
        """
        LOG.debug(f"Got {message.msg_type}: {message.data}")
        if message.msg_type == "neon.audio_input.response":
            self._transcribed = message.data.get("transcripts", [""])[0]

    def handle_klat_response(self, message: Message):
        """
        Handle a valid response from Neon. This includes text and base64-encoded
        audio in all requested languages.
        @param message: Neon response message
        """
        LOG.debug(f"gradio context={message.context['gradio']}")
        resp_data = message.data["responses"]
        files = []
        sentences = []
        session = message.context["gradio"]["session"]
        for _, response in resp_data.items():  # lang, response
            sentences.append(response.get("sentence"))
            if response.get("audio"):
                for _, data in response["audio"].items():
                    # filepath = "/".join(
                    #     [self.audio_cache_dir] + response[gender].split("/")[-4:]
                    # )
                    # TODO: This only plays the most recent, so it doesn't
                    #  support multiple languages or multi-utterance responses
                    self._current_tts[session] = data
                    # files.append(filepath)
                    # if not isfile(filepath):
                    # decode_base64_string_to_file(data, filepath)
        self._response = "\n".join(sentences)
        self._await_response.set()

    def send_audio(
        self,
        audio_b64_string: str,
        lang: str = "en-us",
        username: Optional[str] = None,
        user_profiles: Optional[list] = None,
        context: Optional[dict] = None,
    ):
        """
        Optionally override this to queue audio inputs or do any pre-parsing
        :param audio_file: path to audio file to send to speech module
        :param lang: language code associated with request
        :param username: username associated with request
        :param user_profiles: user profiles expecting a response
        :param context: Optional dict context to add to emitted message
        """
        audio_path = decode_base64_string_to_file(
            audio_b64_string,
            join(f"{self._audio_path}/{time()}.wav"),
        )
        self._send_audio(
            audio_file=audio_path,
            lang=lang,
            username=username,
            user_profiles=user_profiles,
            context=context,
        )

    @property
    def supported_languages(self) -> List[str]:
        """
        Get a list of supported languages from configuration
        @returns: list of BCP-47 language codes
        """
        return self.config.get("languages") or [self.default_lang]

    def _start_session(self):
        sid = uuid4().hex
        self._current_tts[sid] = None
        self._profiles[sid] = self.user_config
        self._profiles[sid]["user"]["username"] = sid
        return sid

    def build_routes(self):
        """Build the FastAPI routes."""

        @self.router.get("/")
        async def read_root(request: Request):
            """Render the Neon AI Web UI and Voice Satellite."""
            description = self.config.get("webui_description", "Chat With Neon")
            title = self.config.get("webui_title", "Neon AI")
            placeholder = self.config.get("webui_input_placeholder", "Ask me something")

            context = {
                "request": request,
                "title": title,
                "description": description,
                "placeholder": placeholder,
            }
            return self.templates.TemplateResponse("index.html", context)

        @self.router.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """Handles websocket connections to OpenWakeWord, which runs as part of this service."""
            await websocket.accept()
            # Send loaded models
            await websocket.send_text(
                json.dumps({"loaded_models": list(self.oww_model.models.keys())})
            )

            while True:
                message = await websocket.receive()

                if message["type"] == "websocket.disconnect":
                    break

                if message["type"] == "websocket.receive":
                    if "text" in message:
                        # Process text message
                        sample_rate = int(message["text"])
                    elif "bytes" in message:
                        # Process bytes message
                        audio_bytes = message["bytes"]

                        # Add extra bytes of silence if needed
                        if len(audio_bytes) % 2 == 1:
                            audio_bytes += b"\x00"

                        # Convert audio to correct format and sample rate
                        audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
                        if sample_rate != 16000:
                            audio_data = resampy.resample(
                                audio_data, sample_rate, 16000
                            )

                        # Get openWakeWord predictions and send to browser client
                        predictions = self.oww_model.predict(audio_data)

                        activations = [
                            key for key, value in predictions.items() if value >= 0.5
                        ]

                        if activations:
                            await websocket.send_text(
                                json.dumps({"activations": activations})
                            )

        @self.router.post("/user_input")
        async def on_user_input_worker(
            req: UserInput,
        ):
            """
            Callback to handle textual user input
            @param utterance: String utterance submitted by the user
            @returns: Session ID, audio input, audio output
            """
            utterance = req.utterance or ""
            audio_input = req.audio_input or ""
            session_id = req.session_id or "websat0000"

            chat_history = []
            input_time = time()
            LOG.debug("Input received")
            if not self._profiles.get("session_id"):
                self._profiles[session_id] = {
                    "speech": {"stt_language": self.default_lang}
                }
                self._current_tts[session_id] = None
            if not self._await_response.wait(30):
                LOG.error("Previous response not completed after 30 seconds")
            in_queue = time() - input_time
            self._await_response.clear()
            self._response = None
            self._transcribed = None
            lang = self.get_lang(session_id)
            if utterance:
                LOG.info(f"Sending utterance: {utterance} with lang: {lang}")
                self.send_utterance(
                    utterance,
                    lang,
                    username=session_id,
                    user_profiles=[self._profiles[session_id]],
                    context={
                        "gradio": {"session": session_id},
                        "timing": {"wait_in_queue": in_queue, "gradio_sent": time()},
                    },
                )
            else:
                LOG.info(f"Sending audio: {audio_input} with lang: {lang}")
                self.send_audio(
                    audio_input,
                    lang,
                    username=session_id,
                    user_profiles=[self._profiles[session_id]],
                    context={
                        "gradio": {"session": session_id},
                        "timing": {"wait_in_queue": in_queue, "gradio_sent": time()},
                    },
                )
                chat_history.append(((audio_input, None), None))
            if not self._await_response.wait(30):
                LOG.error("No response received after 30s")
                self._await_response.set()
            self._response = self._response or "ERROR"
            LOG.info(f"Got response={self._response}")
            if utterance:
                chat_history.append((utterance, self._response))
            elif isinstance(self._transcribed, str):
                LOG.info(f"Got transcript: {self._transcribed}")
                chat_history.append((self._transcribed, self._response))
                utterance = self._transcribed
            resp = UserInputResponse(
                **{
                    "utterance": utterance,
                    "audio_output": self._current_tts[session_id],
                    "session_id": session_id,
                    "transcription": self._response,
                }
            )
            return resp


app = FastAPI()
neon_client = WebSatNeonClient()
app.mount(
    "/static",
    StaticFiles(directory="neon_iris/static"),
    name="Neon Web Voice Satellite",
)
app.include_router(neon_client.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
