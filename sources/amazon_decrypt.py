import re
from typing import Optional, List, Tuple
from Crypto.Cipher import AES
from Crypto.Util import Counter

FALLBACK_STREAMINFO = bytes([
    0x80, 0x00, 0x00, 0x22, 0x10, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0a, 0xc4, 0x42, 0xf0,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00
])

class Mp4CencDecryptor:
    def __init__(self, key_hex: str, target_codec: str = 'flac'):
        self.key = bytes.fromhex(key_hex)
        self.target_codec = target_codec.lower()
        self.default_sample_size = 0
        self.sample_sizes: List[int] = []
        self.sample_ivs: List[bytes] = []
        self.dfla_metadata: Optional[bytes] = None
        self.flac_header_written = False

    def decrypt(self, input_bytes: bytes) -> bytes:
        data = bytearray(input_bytes)
        output = bytearray()
        
        # We perform a linear scan of top level boxes
        offset = 0
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset:offset+4], 'big')
            box_type = data[offset+4:offset+8]
            header_size = 8
            
            if size == 1:
                if offset + 16 > len(data):
                    break
                size = int.from_bytes(data[offset+8:offset+16], 'big')
                header_size = 16
            elif size == 0:
                size = len(data) - offset

            box_end = offset + size
            if box_end > len(data):
                # Out of bounds or incomplete file
                box_end = len(data)

            payload = data[offset+header_size:box_end]
            
            # Process box
            self._process_box(data, offset, header_size, box_type, payload, output)
            offset = box_end
            
        if self.target_codec == 'flac':
            return bytes(output)
        else:
            return bytes(data)

    def _process_box(self, data: bytearray, offset: int, header_size: int, box_type: bytes, payload: bytearray, output: bytearray):
        # Container boxes that we want to parse inside
        if box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'moof', b'traf'):
            inner_offset = 0
            while inner_offset + 8 <= len(payload):
                inner_size = int.from_bytes(payload[inner_offset:inner_offset+4], 'big')
                inner_type = payload[inner_offset+4:inner_offset+8]
                inner_header_size = 8
                
                if inner_size == 1:
                    if inner_offset + 16 > len(payload):
                        break
                    inner_size = int.from_bytes(payload[inner_offset+8:inner_offset+16], 'big')
                    inner_header_size = 16
                elif inner_size == 0:
                    inner_size = len(payload) - inner_offset
                
                inner_end = inner_offset + inner_size
                if inner_end > len(payload):
                    inner_end = len(payload)
                
                inner_payload = payload[inner_offset+inner_header_size:inner_end]
                
                # Recursively process child boxes
                box_abs_offset = offset + header_size + inner_offset
                self._process_box(data, box_abs_offset, inner_header_size, inner_type, inner_payload, output)
                inner_offset = inner_end
            return

        # Specific box handlers
        if box_type == b'stsd':
            self._handle_stsd(data, offset, header_size, payload)
        elif box_type == b'tfhd':
            self._handle_tfhd(payload)
        elif box_type == b'trun':
            self._handle_trun(payload)
        elif box_type == b'senc':
            self._handle_senc(data, offset, header_size, payload)
        elif box_type == b'mdat':
            self._handle_mdat(data, offset, header_size, payload, output)
        elif box_type in (b'sinf', b'sbgp', b'sgpd', b'pssh'):
            # Strip DRM boxes by renaming to 'free' in place
            if self.target_codec != 'flac':
                data[offset+4:offset+8] = b'free'

    def _handle_stsd(self, data: bytearray, offset: int, header_size: int, payload: bytearray):
        # stsd is a FullBox: size/type + version/flags (4 bytes) + entry_count (4 bytes)
        # Search for 'enca' and replace with 'fLaC' or 'mp4a'
        for i in range(8, len(payload) - 4):
            if payload[i:i+4] == b'enca':
                enc_offset = offset + header_size + i
                if self.target_codec == 'flac':
                    data[enc_offset:enc_offset+4] = b'fLaC'
                else:
                    data[enc_offset:enc_offset+4] = b'mp4a'
            elif payload[i:i+4] == b'sinf':
                # Rename sinf inside stsd to free
                sinf_offset = offset + header_size + i
                data[sinf_offset:sinf_offset+4] = b'free'

        # Extract dfLa metadata
        dfla_idx = payload.find(b'dfLa')
        if dfla_idx != -1:
            size = int.from_bytes(payload[dfla_idx-4:dfla_idx], 'big')
            if dfla_idx - 4 + size <= len(payload):
                # Skip dfLa (4 bytes) and version/flags (4 bytes)
                self.dfla_metadata = bytes(payload[dfla_idx+8 : dfla_idx-4+size])

    def _handle_tfhd(self, payload: bytearray):
        flags = int.from_bytes(payload[1:4], 'big')
        offset = 8  # Skip version/flags (4 bytes) + track_ID (4 bytes)
        if flags & 0x000001: offset += 8  # base_data_offset
        if flags & 0x000002: offset += 4  # sample_description_index
        if flags & 0x000008: offset += 4  # default_sample_duration
        if flags & 0x000010:
            self.default_sample_size = int.from_bytes(payload[offset:offset+4], 'big')

    def _handle_trun(self, payload: bytearray):
        flags = int.from_bytes(payload[1:4], 'big')
        sample_count = int.from_bytes(payload[4:8], 'big')
        
        data_offset_present = bool(flags & 0x000001)
        first_sample_flags_present = bool(flags & 0x000004)
        sample_duration_present = bool(flags & 0x000100)
        sample_size_present = bool(flags & 0x000200)
        sample_flags_present = bool(flags & 0x000400)
        
        offset = 8
        if data_offset_present: offset += 4
        if first_sample_flags_present: offset += 4
        
        self.sample_sizes = []
        for _ in range(sample_count):
            if sample_duration_present: offset += 4
            if sample_size_present:
                self.sample_sizes.append(int.from_bytes(payload[offset:offset+4], 'big'))
                offset += 4
            else:
                self.sample_sizes.append(self.default_sample_size)
            if sample_flags_present: offset += 4

    def _handle_senc(self, data: bytearray, offset: int, header_size: int, payload: bytearray):
        # Rename senc box to free in place for MP4 output
        if self.target_codec != 'flac':
            data[offset+4:offset+8] = b'free'
            
        flags = int.from_bytes(payload[1:4], 'big')
        sample_count = int.from_bytes(payload[4:8], 'big')
        iv_size = 8  # Amazon Music uses 8-byte IVs
        
        idx = 8
        self.sample_ivs = []
        for _ in range(sample_count):
            iv = bytearray(16)
            iv[:8] = payload[idx:idx+8]
            self.sample_ivs.append(bytes(iv))
            idx += iv_size
            
            if flags & 0x000002: # Subsample encryption
                subsample_count = int.from_bytes(payload[idx:idx+2], 'big')
                idx += 2 + subsample_count * 6

    def _handle_mdat(self, data: bytearray, offset: int, header_size: int, payload: bytearray, output: bytearray):
        # Write FLAC header if extracting raw FLAC
        if self.target_codec == 'flac' and not self.flac_header_written:
            output.extend(b'fLaC')
            output.extend(self.dfla_metadata or FALLBACK_STREAMINFO)
            self.flac_header_written = True

        # Process each sample in mdat
        sample_offset = 0
        for i, sample_size in enumerate(self.sample_sizes):
            if sample_offset + sample_size > len(payload):
                break
                
            sample_data = payload[sample_offset : sample_offset + sample_size]
            
            # Decrypt if we have an IV
            if i < len(self.sample_ivs):
                iv = self.sample_ivs[i]
                ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'))
                cipher = AES.new(self.key, AES.MODE_CTR, counter=ctr)
                decrypted_sample = cipher.decrypt(sample_data)
            else:
                decrypted_sample = sample_data

            if self.target_codec == 'flac':
                output.extend(decrypted_sample)
            else:
                # Decrypt in place inside the mdat payload in the main bytearray
                abs_sample_start = offset + header_size + sample_offset
                data[abs_sample_start : abs_sample_start + sample_size] = decrypted_sample
                
            sample_offset += sample_size
