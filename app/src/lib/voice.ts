/**
 * Voice in and out, using only what the browser already has.
 *
 * No dependency, no API key, no audio leaving the machine: SpeechSynthesis
 * speaks locally, and SpeechRecognition in Chrome does send audio to Google —
 * which is why `listen()` is opt-in per press and never runs on its own. An
 * agent console that quietly held an open microphone would be a worse thing
 * than it is useful.
 *
 * Every entry point is capability-checked and degrades to "no voice" rather
 * than throwing. This is a progressive enhancement on a working text UI.
 */

type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
};

type SpeechWindow = Window & {
  SpeechRecognition?: new () => Recognition;
  webkitSpeechRecognition?: new () => Recognition;
};

export function canSpeak(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function canListen(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as SpeechWindow;
  return Boolean(w.SpeechRecognition ?? w.webkitSpeechRecognition);
}

/**
 * Speak one utterance, replacing anything already speaking.
 *
 * The agent's text is model-authored and can be long; a page that queues four
 * minutes of speech the user cannot interrupt is unusable, so each call cancels
 * the previous one and the text is clipped.
 */
export function speak(text: string): void {
  if (!canSpeak()) return;
  const trimmed = text.trim();
  if (!trimmed) return;
  try {
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(trimmed.slice(0, 700));
    utterance.rate = 1.05;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
  } catch {
    // A browser that refuses to speak must not break the conversation.
  }
}

export function stopSpeaking(): void {
  if (!canSpeak()) return;
  try {
    window.speechSynthesis.cancel();
  } catch {
    /* nothing to stop */
  }
}

/**
 * Listen for a single utterance and hand back the transcript.
 *
 * `continuous` is false deliberately: one press, one sentence, microphone
 * closes. Returns a stop function so a component unmounting mid-listen does not
 * leave the microphone open.
 */
export function listen(
  onText: (text: string) => void,
  onDone: (error?: string) => void,
): () => void {
  if (typeof window === "undefined") {
    onDone("unsupported");
    return () => {};
  }
  const w = window as SpeechWindow;
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  if (!Ctor) {
    onDone("unsupported");
    return () => {};
  }

  let stopped = false;
  const recognition = new Ctor();
  recognition.lang = "en-US";
  recognition.interimResults = false;
  recognition.continuous = false;
  recognition.maxAlternatives = 1;

  recognition.onresult = (event) => {
    const first = event.results?.[0]?.[0]?.transcript;
    if (typeof first === "string" && first.trim()) onText(first.trim());
  };
  recognition.onerror = (event) => {
    // "no-speech" and "aborted" are ordinary outcomes, not failures to report.
    const code = event?.error;
    onDone(code === "no-speech" || code === "aborted" ? undefined : (code ?? "error"));
  };
  recognition.onend = () => {
    if (!stopped) onDone();
  };

  try {
    recognition.start();
  } catch {
    onDone("error");
    return () => {};
  }

  return () => {
    stopped = true;
    try {
      recognition.stop();
    } catch {
      /* already stopped */
    }
  };
}
