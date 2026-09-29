// Camera capture for face registration (KYC) and face confirmation.
// Frames are JPEGs sent once to the gateway, which analyses them in memory,
// keeps only their hashes, and discards the images.

const FRAME_COUNT = 10;
const FRAME_INTERVAL_MS = 350;
const MAX_WIDTH = 640;

function coded(reasonCode, message) {
  const error = new Error(message || reasonCode);
  error.reasonCode = reasonCode;
  return error;
}

export async function openCamera(video) {
  if (!navigator.mediaDevices?.getUserMedia) throw coded("CAMERA_UNAVAILABLE");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {facingMode: "user", width: {ideal: 640}, height: {ideal: 480}},
    });
  } catch (error) {
    throw coded(error?.name === "NotAllowedError" ? "CAMERA_DENIED" : "CAMERA_UNAVAILABLE");
  }
  video.srcObject = stream;
  video.muted = true;
  video.playsInline = true;
  await video.play().catch(() => {});
  return () => {
    stream.getTracks().forEach(track => track.stop());
    video.srcObject = null;
  };
}

// The prompts walk the user through a head turn: liveness needs the head to
// turn in 3D, which a photo or a screen held up to the camera cannot do.
export function promptFor(index) {
  if (index < 2) return "Look straight at the camera…";
  if (index < 7) return "Now slowly turn your head to one side…";
  return "…and back to the centre.";
}

export async function captureHeadTurn(video, {onStep = () => {}} = {}) {
  if (!video.videoWidth) {
    await new Promise(resolve => { video.onloadedmetadata = resolve; setTimeout(resolve, 2000); });
  }
  if (!video.videoWidth) throw coded("CAMERA_UNAVAILABLE");
  const scale = Math.min(1, MAX_WIDTH / video.videoWidth);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  const context = canvas.getContext("2d");
  const frames = [];
  for (let index = 0; index < FRAME_COUNT; index += 1) {
    onStep(index, promptFor(index));
    await new Promise(resolve => setTimeout(resolve, FRAME_INTERVAL_MS));
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    frames.push(canvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1]);
  }
  return frames;
}

export const faceMessages = {
  FACE_NO_MATCH: "That face does not match the one registered for this account.",
  FACE_NOT_LIVE: "Turn your head a little further to one side during the scan. A photo cannot do this.",
  FACE_NOT_FOUND: "No face was seen. Face the camera in good light.",
  FACE_MULTIPLE_FACES: "More than one face is in view. Make sure only you are in the frame.",
  FACE_INCONSISTENT: "The face changed during the scan. Please try again.",
  FACE_REPLAYED: "Those camera frames were already used. Please scan again.",
  FACE_LOCKED: "Too many failed scans. Face confirmation is locked for 10 minutes.",
  FACE_ENGINE_UNAVAILABLE: "Face recognition is not installed. Run install.cmd again.",
  FACE_DOES_NOT_MATCH_ACCOUNT: "This account is linked to a different face.",
  FACE_FRAME_INVALID: "The camera image could not be read. Please try again.",
  CAMERA_DENIED: "Allow the camera for this site (the icon next to the address bar), then try again.",
  CAMERA_UNAVAILABLE: "No camera was found. Face confirmation needs a camera.",
  APPROVAL_CHALLENGE_EXPIRED: "This confirmation expired. Cancel and start the approval again.",
  KYC_CONSENT_EXPIRED: "The Myinfo consent expired. Start again.",
};

export async function postFrames(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const reason = typeof payload.detail === "string" ? payload.detail : `HTTP_${response.status}`;
    throw coded(reason, faceMessages[reason] || reason.replaceAll("_", " ").toLowerCase());
  }
  return payload;
}
