const URL = "ws://localhost:9083";
const video = document.getElementById("videoElement");
const canvas = document.getElementById("canvasElement");
let context;

// Initialize context here
window.addEventListener("load", () => {
	context = canvas.getContext("2d");
	setInterval(captureImage, 3000);
});

const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const mediaRecorder = null;
let stream = null;
let currentFrameB64;
let webSocket = null;
let audioContext = null;
let processor = null;
let pcmData = [];
let interval = null;
let initialized = false;
let audioInputContext;
let workletNode;

// start screen capture
async function startScreenShare() {
	try {
		stream = await navigator.mediaDevices.getDisplayMedia({
			video: {
				width: { max: 640 },
				height: { max: 480 },
			},
		});

		video.srcObject = stream;
		await new Promise((resolve) => {
			video.onloadedmetadata = () => {
				console.log("video loaded metadata");
				resolve();
			};
		});
	} catch (err) {
		console.error("Error accessing the screen: ", err);
	}
}

// capture an image from the shared screen
function captureImage() {
	if (stream && video.videoWidth > 0 && video.videoHeight > 0 && context) {
		canvas.width = 640;
		canvas.height = 480;
		context.drawImage(video, 0, 0, canvas.width, canvas.height);
		const imageData = canvas.toDataURL("image/jpeg").split(",")[1].trim();
		currentFrameB64 = imageData;
	} else {
		console.log("no stream or video metadata not loaded");
	}
}

window.addEventListener("load", async () => {
	try {
		await startScreenShare();
		await initializeAudioContext();

		// Initialize canvas context
		context = canvas.getContext("2d");
		if (!context) {
			throw new Error("Could not get canvas context");
		}

		// Start capture interval after everything is initialized
		setInterval(captureImage, 3000);

		// Attempt WebSocket connection
		connect();
	} catch (error) {
		console.error("Initialization error:", error);
	}
});

function connect() {
	console.log("Attempting to connect to:", URL);

	webSocket = new WebSocket(URL);

	webSocket.onopen = (event) => {
		console.log("WebSocket connection established");
		// Only send setup message after connection is confirmed
		sendInitialSetupMessage();
	};

	webSocket.onclose = (event) => {
		console.log("WebSocket closed. Code:", event.code, "Reason:", event.reason);
		if (event.code === 1006) {
			console.log("Abnormal closure, attempting to reconnect in 5 seconds...");
			setTimeout(connect, 5000);
		}
	};

	webSocket.onerror = (error) => {
		console.error("WebSocket error:", error);
	};

	webSocket.onmessage = receiveMessage;
}

function sendInitialSetupMessage() {
	if (webSocket.readyState !== WebSocket.OPEN) {
		console.error(
			"WebSocket is not open. Current state:",
			webSocket.readyState,
		);
		return;
	}

	const setup_client_message = {
		setup: {
			generation_config: {
				response_modalities: ["AUDIO", "TEXT"],
			},
		},
	};

	console.log("Sending setup message:", setup_client_message);
	try {
		webSocket.send(JSON.stringify(setup_client_message));
		console.log("Setup message sent successfully");
	} catch (error) {
		console.error("Error sending setup message:", error);
	}
}

function sendVoiceMessage(b64PCM) {
	if (webSocket == null) {
		console.log("websocket not initialized");
		return;
	}

	payload = {
		realtime_input: {
			media_chunks: [
				{
					mime_type: "audio/pcm",
					data: b64PCM,
				},
				{
					mime_type: "image/jpeg",
					data: currentFrameB64,
				},
			],
		},
	};

	webSocket.send(JSON.stringify(payload));
	console.log("sent: ", payload);
}

function receiveMessage(event) {
	const messageData = JSON.parse(event.data);
	const response = new Response(messageData);

	if (response.text) {
		displayMessage(`GEMINI: ${response.text}`);
	}
	if (response.audioData) {
		injestAudioChuckToPlay(response.audioData);
	}
}

async function initializeAudioContext() {
	if (initialized) {
		return;
	}

	audioInputContext = new (window.AudioContext || window.webkitAudioContext)({
		sampleRate: 24000,
	});
	await audioInputContext.audioWorklet.addModule("pcm-processor.js");
	workletNode = new AudioWorkletNode(audioInputContext, "pcm-processor");
	workletNode.connect(audioInputContext.destination);
	initialized = true;
}

function base64ToArrayBuffer(base64) {
	const binaryString = window.atob(base64);
	const bytes = new Uint8Array(binaryString.length);
	for (let i = 0; i < binaryString.length; i++) {
		bytes[i] = binaryString.charCodeAt(i);
	}
	return bytes.buffer;
}

function convertPCM16LEToFloat32(pcmData) {
	const inputArray = new Int16Array(pcmData);
	const float32Array = new Float32Array(inputArray.length);

	for (let i = 0; i < inputArray.length; i++) {
		float32Array[i] = inputArray[i] / 32768;
	}

	return float32Array;
}

async function injestAudioChuckToPlay(base64AudioChunk) {
	try {
		if (audioInputContext.state === "suspended") {
			await audioInputContext.resume();
		}
		const arrayBuffer = base64ToArrayBuffer(base64AudioChunk);
		const float32Data = convertPCM16LEToFloat32(arrayBuffer);

		workletNode.port.postMessage(float32Data);
	} catch (error) {
		console.error("Error processing audio chunk:", error);
	}
}

function recordChunk() {
	const buffer = new ArrayBuffer(pcmData.length * 2);
	const view = new DataView(buffer);
	pcmData.forEach((value, index) => {
		view.setInt16(index * 2, value, true);
	});

	const base64 = btoa(String.fromCharCode.apply(null, new Uint8Array(buffer)));

	sendVoiceMessage(base64);
	pcmData = [];
}

async function startAudioInput() {
	audioContext = new AudioContext({
		sampleRate: 16000,
	});

	const stream = await navigator.mediaDevices.getUserMedia({
		audio: {
			channelCount: 1,
			sampleRate: 16000,
		},
	});

	const source = audioContext.createMediaStreamSource(stream);
	processor = audioContext.createScriptProcessor(4096, 1, 1);

	processor.onaudioprocess = (e) => {
		const inputData = e.inputBuffer.getChannelData(0);
		const pcm16 = new Int16Array(inputData.length);
		for (let i = 0; i < inputData.length; i++) {
			pcm16[i] = inputData[i] * 0x7fff;
		}
		pcmData.push(...pcm16);
	};

	source.connect(processor);
	processor.connect(audioContext.destination);

	interval = setInterval(recordChunk, 3000);
}

function stopAudioInput() {
	if (processor) {
		processor.disconnect();
	}
	if (audioContext) {
		audioContext.close();
	}

	clearInterval(interval);
}

function displayMessage(message) {
	console.log(message);
	addParagraphToDiv("chatLog", message);
}

function addParagraphToDiv(divId, text) {
	const newParagraph = document.createElement("p");
	newParagraph.textContent = text;
	const div = document.getElementById(divId);
	div.appendChild(newParagraph);
}

startButton.addEventListener("click", startAudioInput);
stopButton.addEventListener("click", stopAudioInput);

class Response {
	constructor(data) {
		this.text = null;
		this.audioData = null;
		this.endOfTurn = null;

		if (data.text) {
			this.text = data.text;
		}

		if (data.audio) {
			this.audioData = data.audio;
		}
	}
}
