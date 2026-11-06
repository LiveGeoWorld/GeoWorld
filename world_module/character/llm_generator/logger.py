"""Generation logger — records LLM prompts and responses per step.

Creates a timestamped log file in llm_generator/log/ for debugging.
Format: 生成_05.19_16.34.log
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


_LOG_DIR = Path(__file__).resolve().parent / "log"


class GenerationLogger:
    """Writes structured step-by-step logs for LLM character generation.

    Usage:
        logger = GenerationLogger()
        logger.log_header(world_path, count, model, provider)
        logger.log_step_prompt("STEP 1", prompt, extra)
        logger.log_step_response(response_text, summary)
        ... (more steps) ...
        logger.log_summary(char_count, rel_count, duration, output_path)
        logger.close()
    """

    def __init__(self, log_dir: str | Path | None = None):
        self._log_dir = Path(log_dir) if log_dir else _LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now()
        name = f"生成_{timestamp:%m.%d_%H.%M}.log"
        self._path = self._log_dir / name
        self._fh = open(self._path, "w", encoding="utf-8")
        self._created_at = timestamp

    def log_header(self, world_path: str, count: int, model: str, provider: str):
        self._write_hr("=")
        self._write(f"LLM Generator Log — {self._created_at:%Y-%m-%d %H:%M:%S}")
        self._write(f"World:       {world_path}")
        self._write(f"Count:       {count}")
        self._write(f"Model:       {model}")
        self._write(f"Provider:    {provider}")
        self._write(f"Log file:    {self._path}")
        self._write_hr("=")

    def log_step_prompt(self, step_name: str, prompt: str, extra_info: str = ""):
        """Log a prompt sent to LLM."""
        size = len(prompt)
        header = step_name
        if extra_info:
            header += f"  [{extra_info}]"
        else:
            header += f"  [{size} chars]"
        self._write_hr("-")
        self._write(header)
        self._write_hr("-")
        self._write("[PROMPT] " + "-" * 74)
        self._write(prompt)
        self._write("[PROMPT END] " + "-" * 70)
        self._write("")

    def log_step_response(self, response_text: str, parsed_summary: str = ""):
        """Log an LLM response and its parsed result."""
        self._write("[RESPONSE] " + "-" * 73)
        self._write(response_text[:8000])  # truncate huge responses
        if len(response_text) > 8000:
            self._write(f"... (truncated, total {len(response_text)} chars)")
        self._write("[RESPONSE END] " + "-" * 69)
        if parsed_summary:
            self._write(f"[PARSED] → {parsed_summary}")
        self._write("")

    def log_step_info(self, message: str):
        """Log a pure informational message."""
        self._write_hr("-")
        self._write(message)
        self._write("")

    def log_summary(
        self,
        char_count: int,
        rel_count: int,
        duration_sec: float,
        output_path: str,
    ):
        self._write_hr("=")
        self._write("SUMMARY")
        self._write(f"  Characters:    {char_count}")
        self._write(f"  Relationships: {rel_count}")
        self._write(f"  Duration:      {duration_sec:.1f}s")
        self._write(f"  Output:        {output_path}")
        self._write_hr("=")

    def close(self):
        if self._fh and not self._fh.closed:
            self._fh.close()

    # ── internal ──

    def _write(self, text: str):
        self._fh.write(text + "\n")

    def _write_hr(self, char: str):
        self._fh.write(char * 80 + "\n")
