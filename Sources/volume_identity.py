#!/usr/bin/env python3
"""Read only mounted-volume identity via macOS Disk Arbitration."""

import ctypes
import json
import os
import sys


def identity(path):
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    da = ctypes.CDLL("/System/Library/Frameworks/DiskArbitration.framework/DiskArbitration")
    pointer = ctypes.c_void_p
    length = ctypes.c_long
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [pointer, ctypes.c_char_p, length, ctypes.c_bool]
    cf.CFURLCreateFromFileSystemRepresentation.restype = pointer
    cf.CFDictionaryGetValue.argtypes = [pointer, pointer]
    cf.CFDictionaryGetValue.restype = pointer
    cf.CFUUIDCreateString.argtypes = [pointer, pointer]
    cf.CFUUIDCreateString.restype = pointer
    cf.CFStringGetCString.argtypes = [pointer, pointer, length, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFURLGetFileSystemRepresentation.argtypes = [pointer, ctypes.c_bool, pointer, length]
    cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [pointer]
    cf.CFRelease.restype = None
    da.DASessionCreate.argtypes = [pointer]
    da.DASessionCreate.restype = pointer
    da.DADiskCreateFromVolumePath.argtypes = [pointer, pointer, pointer]
    da.DADiskCreateFromVolumePath.restype = pointer
    da.DADiskCopyDescription.argtypes = [pointer]
    da.DADiskCopyDescription.restype = pointer
    owned = []

    def own(value):
        if not value:
            raise RuntimeError("系统未返回可核实的卷身份")
        owned.append(value)
        return value

    try:
        encoded = os.fsencode(path)
        url = own(cf.CFURLCreateFromFileSystemRepresentation(None, encoded, len(encoded), True))
        session = own(da.DASessionCreate(None))
        disk = own(da.DADiskCreateFromVolumePath(None, session, url))
        description = own(da.DADiskCopyDescription(disk))
        uuid_key = pointer.in_dll(da, "kDADiskDescriptionVolumeUUIDKey").value
        path_key = pointer.in_dll(da, "kDADiskDescriptionVolumePathKey").value
        uuid = cf.CFDictionaryGetValue(description, uuid_key)
        volume_url = cf.CFDictionaryGetValue(description, path_key)
        if not uuid or not volume_url:
            raise RuntimeError("卷 UUID 或挂载路径缺失")
        uuid_string = own(cf.CFUUIDCreateString(None, uuid))
        uuid_buffer = ctypes.create_string_buffer(128)
        path_buffer = ctypes.create_string_buffer(4096)
        if not cf.CFStringGetCString(uuid_string, uuid_buffer, len(uuid_buffer), 0x08000100):
            raise RuntimeError("卷 UUID 无法读取")
        if not cf.CFURLGetFileSystemRepresentation(volume_url, True, path_buffer, len(path_buffer)):
            raise RuntimeError("卷挂载路径无法读取")
        return {"VolumeUUID": uuid_buffer.value.decode("ascii"), "MountPoint": os.fsdecode(path_buffer.value)}
    finally:
        for value in reversed(owned):
            cf.CFRelease(value)


if __name__ == "__main__":
    try:
        print(json.dumps(identity(sys.argv[1]), ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
