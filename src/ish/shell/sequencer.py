from __future__ import annotations

from typing import Optional, Dict, Tuple, Set, Callable

__all__ = ['Sequencer']


class Sequencer:
	GROUND: int = 0
	CARET: int = 1
	ESC: int = 2
	CSI: int = 3
	OSC: int = 4
	BETWEEN: int = 5
	MASKING: int = 6

	ESC_SINGLE_STARTS: Set = {
		0x50,
		0x52,
		0x53,
		0x56,
		0x57,
		0x58,
		0x5D,
		0x5E,
		0x5F,
	}
	ESC_SINGLE_INTERS: Set = {
		0x28,
		0x29,
		0x2A,
		0x2B,
		0x2C,
		0x2D,
		0x2E,
		0x2F,
		0x5B,
	}
	ESC_SINGLE_FINALS: Set = {
		0x37, 0x38,
		0x3C, 0x3D, 0x3E,
		0x40, 0x41, 0x42, 0x43, 0x44, 0x45,
		0x4C, 0x4D, 0x4E, 0x4F,
		0x5A,
		0x5C,
	}

	__slots__ = (
		'buffer', 'between_buffer', 'masking_buffer', 'state',
		'masking_index', 'masking_disabled', 'masking_bytes',
		'encoder', 'callbacks', 'between_callbacks', 'active_between',
		'max_sequence_bytes', '_between_tail', 'between_truncated', '_discard_control', 'prefix_callbacks', 'literal_starts'
	)

	def __init__(self, encoder: str = 'utf-8', errors: str = 'replace', max_sequence_bytes: int = 65536):
		if max_sequence_bytes < 64:
			raise ValueError('Sequence limit must be at least 64 bytes')
		self.max_sequence_bytes = max_sequence_bytes
		self._between_tail = bytearray()
		self.between_truncated = False
		self._discard_control = False
		self.encoder = encoder
		self.buffer: bytearray = bytearray()
		self.between_buffer: bytearray = bytearray()
		self.masking_buffer: bytearray = bytearray()
		self.masking_bytes: bytes = b""
		self.state: int = self.GROUND

		self.callbacks: Dict[bytes, Tuple[Callable, bool]] = {}
		self.prefix_callbacks = {}
		self.literal_starts = {}
		self.between_callbacks: Dict[bytes, Tuple[bytes, Callable, bool]] = {}
		self.active_between: Optional[Tuple[bytes, bytes, Callable, bool]] = None

		self.masking_index: int = 0
		self.masking_disabled: bool = False

	def on_sequence(self, seq: bytes, callback: Callable, remove_seq: bool = True) -> None:
		self.callbacks[seq] = (callback, remove_seq)

	def on_prefix(self, prefix: bytes, callback: Callable) -> None:
		self.prefix_callbacks[prefix] = callback

	def between_sequence(
		self,
		start_seq: bytes,
		end_seq: bytes,
		callback: Callable,
		remove_seq: bool = True,
	) -> None:
		"""Capture a delimited payload; a callback may return bytes to display.

		Replacement bytes are emitted in stream order when remove_seq is true.
		"""
		self.between_callbacks[start_seq] = (end_seq, callback, remove_seq)
		if start_seq and start_seq[0] != 0x1b:
			self.literal_starts.setdefault(start_seq[0], set()).add(start_seq)

	def at_masking(self, byte: bytes) -> None:
		# Large commands still execute, but their echo is not retained for masking.
		self.masking_bytes = bytes(byte) if len(byte) <= self.max_sequence_bytes else b''
		self.masking_buffer.clear()
		self.masking_index = 0
		self.masking_disabled = False

	def _flush_text(self, buffer: bytearray):
		buffer.extend(self.buffer)
		self.buffer.clear()

	def _union(self, buffer: bytearray) -> None:
		data = bytes(self.buffer)
		for prefix, callback in self.prefix_callbacks.items():
			if data.startswith(prefix):
				callback(data[len(prefix):-2] if data.endswith(b'\x1b\\') else data[len(prefix):-1])
				self.buffer.clear()
				self.state = self.GROUND
				return

		if data in self.between_callbacks:
			end_seq, callback, remove_seq = self.between_callbacks[data]
			self.active_between = (data, end_seq, callback, remove_seq)
			self.between_buffer.clear()
			self._between_tail.clear()
			self.between_truncated = False
			if not remove_seq:
				buffer.extend(self.buffer)
			self.buffer.clear()
			self.state = self.BETWEEN
			return

		if data in self.callbacks:
			callback, remove_seq = self.callbacks[data]
			callback()
			if not remove_seq:
				buffer.extend(self.buffer)
		else:
			buffer.extend(self.buffer)

		self.buffer.clear()
		self.state = self.GROUND

	def _between(self, byte, buffer: bytearray) -> None:
		start_seq, end_seq, callback, remove_seq = self.active_between
		self._between_tail.append(byte)
		if len(self._between_tail) > len(end_seq):
			if len(self.between_buffer) < self.max_sequence_bytes:
				self.between_buffer.append(self._between_tail[0])
			else:
				self.between_truncated = True
			del self._between_tail[0]
		if not remove_seq:
			buffer.append(byte)
		if self._between_tail == end_seq:
			payload = bytes(self.between_buffer)
			if self.between_truncated:
				payload += b' [prompt truncated] '
			replacement = callback(payload)
			if remove_seq and isinstance(replacement, bytes):
				buffer.extend(replacement)
			self.between_buffer.clear()
			self._between_tail.clear()
			if end_seq in self.between_callbacks:
				new_end_seq, new_callback, new_remove_seq = self.between_callbacks[end_seq]
				self.active_between = (end_seq, new_end_seq, new_callback, new_remove_seq)
				self.between_truncated = False
				self.state = self.BETWEEN
			else:
				self.active_between = None
				self.state = self.GROUND

	def _complete_masking(self) -> None:
		self.masking_bytes = b""
		self.masking_buffer.clear()
		self.masking_index = 0
		self.masking_disabled = True
		self.state = self.GROUND

	def _cancel_masking(self) -> None:
		self.buffer.extend(self.masking_buffer)
		self.masking_buffer.clear()
		# This echo did not match. Do not retry against later program output.
		self.masking_bytes = b''
		self.masking_index = 0
		self.masking_disabled = True
		self.state = self.GROUND

	def interpret(self, data: bytes):
		output = bytearray()
		for byte in data:
			if not self._discard_control and len(self.buffer) >= self.max_sequence_bytes:
				if self.state in (self.OSC, self.CSI):
					self.buffer[:] = self.buffer[-1:]
					self._discard_control = True
				else:
					self._flush_text(output)
					self.state = self.GROUND
			if self._discard_control:
				previous = self.buffer[-1:] == b'\x1b'
				self.buffer[:] = bytes([byte])
				if ((self.state == self.CSI and 0x40 <= byte <= 0x7e) or
					(self.state == self.OSC and (byte == 7 or previous and byte == 92))):
					self.buffer.clear()
					self._discard_control = False
					self.state = self.GROUND
				continue
			if self.state == self.GROUND:
				if byte in self.literal_starts:
					self._flush_text(output)
					self.buffer.append(byte)
					self.state = self.CARET
				elif byte == 0x1B:
					self._flush_text(output)
					self.buffer.append(byte)
					self.state = self.ESC
				else:
					if byte == 0x0A and self.masking_disabled:
						self.masking_disabled = False
					if self.masking_bytes and not self.masking_disabled and byte == self.masking_bytes[self.masking_index]:
						if len(self.masking_bytes) == 1:
							self._complete_masking()
						else:
							self.masking_buffer.append(byte)
							self.masking_index += 1
							self.state = self.MASKING
					else:
						self.buffer.append(byte)

			elif self.state == self.CARET:
				self.buffer.append(byte)
				candidates = self.literal_starts[self.buffer[0]]
				if bytes(self.buffer) in candidates:
					self._union(output)
				elif not any(seq.startswith(self.buffer) for seq in candidates):
					# Keep a new introducer at the tail when a prefix mismatches.
					if byte in self.literal_starts or byte == 0x1b:
						output.extend(self.buffer[:-1])
						self.buffer[:] = bytes([byte])
						self.state = self.ESC if byte == 0x1b else self.CARET
					else:
						self._flush_text(output)
						self.state = self.GROUND

			elif self.state == self.ESC:
				self.buffer.append(byte)
				if byte in self.ESC_SINGLE_INTERS:
					self.state = self.CSI
				elif byte in self.ESC_SINGLE_STARTS:
					self.state = self.OSC
				elif byte in self.ESC_SINGLE_FINALS:
					self._union(output)
				elif 0x40 <= byte <= 0x5F:
					self._union(output)

			elif self.state == self.CSI:
				self.buffer.append(byte)
				if 0x40 <= byte <= 0x7E:
					self._union(output)

			elif self.state == self.OSC:
				self.buffer.append(byte)
				if byte == 0x07:
					self._union(output)
				elif len(self.buffer) >= 2 and self.buffer[-2:] == b'\x1b\\':
					self._union(output)

			elif self.state == self.BETWEEN:
				self._between(byte, output)

			elif self.state == self.MASKING:
				if byte == 0x1B:
					self._cancel_masking()
					self._flush_text(output)
					self.buffer.append(byte)
					self.state = self.ESC
				else:
					self.masking_buffer.append(byte)
					if self.masking_index < len(self.masking_bytes) and byte == self.masking_bytes[self.masking_index]:
						self.masking_index += 1
						if self.masking_index == len(self.masking_bytes):
							self._complete_masking()
					else:
						self._cancel_masking()

		if self.state == self.GROUND:
			self._flush_text(output)

		return bytes(output)
