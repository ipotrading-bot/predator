"""
api/logger.py — BetterStack (Logtail) Integration
Centralized logging with remote monitoring for Vercel serverless functions.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import httpx

from config import settings


class BetterStackHandler(logging.Handler):
    """
    Custom logging handler that sends logs to BetterStack (Logtail).
    Compatible with Vercel serverless functions.
    """
    
    def __init__(self, source_token: str = None, source_id: str = None):
        super().__init__()
        self.source_token = source_token or settings.betterstack_token
        self.source_id = source_id or settings.betterstack_source_id
        self.enabled = bool(self.source_token)
        self._buffer = []
        self._buffer_size = 0
        self._max_buffer = 50  # Send in batches of 50
    
    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record."""
        if not self.enabled:
            # Fallback to stdout
            print(self.format(record))
            return
        
        try:
            log_entry = self._format_log(record)
            self._buffer.append(log_entry)
            self._buffer_size += 1
            
            # Send when buffer is full
            if self._buffer_size >= self._max_buffer:
                self._flush_buffer()
                
        except Exception:
            self.handleError(record)
    
    def _format_log(self, record: logging.LogRecord) -> dict:
        """Format log record for BetterStack."""
        return {
            "dt": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "timestamp": record.created,
            "runtime": getattr(record, "runtime", None),
            "session_id": getattr(record, "session_id", None),
            "extra": getattr(record, "extra", {})
        }
    
    def _flush_buffer(self) -> None:
        """Send buffered logs to BetterStack."""
        if not self._buffer:
            return
        
        try:
            headers = {
                "Authorization": f"Bearer {self.source_token}",
                "Content-Type": "application/json"
            }
            
            payload = json.dumps(self._buffer)
            
            # Fire and forget - don't wait for response in serverless
            with httpx.Client() as client:
                client.post(
                    "https://in.logs.betterstack.com",
                    headers=headers,
                    content=payload,
                    timeout=5
                )
            
            self._buffer.clear()
            self._buffer_size = 0
            
        except Exception as e:
            # Log error but don't crash
            print(f"BetterStack flush error: {e}")
    
    def flush(self) -> None:
        """Flush any remaining logs."""
        self._flush_buffer()
    
    def close(self) -> None:
        """Close the handler."""
        self.flush()
        super().close()


class ScanLogger:
    """
    Specialized logger for PAIM scan operations.
    Provides structured logging with session tracking.
    """
    
    def __init__(self, session_id: str = None):
        self.session_id = session_id or f"scan_{int(time.time())}"
        self.logger = self._setup_logger()
        self.start_time = time.time()
        self.steps = []
    
    def _setup_logger(self) -> logging.Logger:
        """Setup logger with BetterStack handler."""
        logger = logging.getLogger(f"paim_scan.{self.session_id}")
        logger.setLevel(logging.DEBUG)
        
        # Add BetterStack handler
        if not logger.handlers:
            handler = BetterStackHandler()
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
            # Also add console handler for local dev
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_formatter = logging.Formatter(
                '%(levelname)s: %(message)s'
            )
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
        
        return logger
    
    def start(self, message: str) -> None:
        """Log scan start."""
        self.steps.append({"step": message, "start": time.time(), "status": "started"})
        self._log_with_context("info", message, {"step": len(self.steps)})
    
    def complete(self, message: str, data: dict = None) -> None:
        """Log step completion."""
        if self.steps:
            self.steps[-1]["status"] = "completed"
            self.steps[-1]["end"] = time.time()
            self.steps[-1]["duration"] = self.steps[-1]["end"] - self.steps[-1]["start"]
        
        self._log_with_context("info", message, data or {})
    
    def error(self, message: str, exc: Exception = None) -> None:
        """Log error."""
        if self.steps:
            self.steps[-1]["status"] = "error"
            self.steps[-1]["error"] = str(exc) if exc else None
        
        extra_data = {"error": str(exc)} if exc else {}
        self._log_with_context("error", message, extra_data)
    
    def warning(self, message: str, data: dict = None) -> None:
        """Log warning."""
        self._log_with_context("warning", message, data or {})
    
    def debug(self, message: str, data: dict = None) -> None:
        """Log debug."""
        self._log_with_context("debug", message, data or {})
    
    def _log_with_context(self, level: str, message: str, data: dict) -> None:
        """Log with session context."""
        extra = {
            "session_id": self.session_id,
            "runtime": round(time.time() - self.start_time, 2),
            "extra": data
        }
        
        log_method = getattr(self.logger, level)
        log_method(message, extra=extra)
    
    def get_summary(self) -> dict:
        """Get scan summary."""
        total_duration = time.time() - self.start_time
        completed_steps = [s for s in self.steps if s["status"] == "completed"]
        error_steps = [s for s in self.steps if s["status"] == "error"]
        
        return {
            "session_id": self.session_id,
            "total_duration": round(total_duration, 2),
            "total_steps": len(self.steps),
            "completed_steps": len(completed_steps),
            "error_steps": len(error_steps),
            "steps": self.steps,
            "completed_at": datetime.now().isoformat()
        }


def get_logger(name: str = "paim") -> logging.Logger:
    """
    Get a configured logger instance.
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        handler = BetterStackHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    return logger


# Pre-configured loggers
scan_logger = None  # Create per-scan with unique session


def create_scan_logger() -> ScanLogger:
    """Create a new scan logger instance."""
    global scan_logger
    scan_logger = ScanLogger()
    return scan_logger


def get_scan_logger() -> Optional[ScanLogger]:
    """Get the current scan logger."""
    return scan_logger