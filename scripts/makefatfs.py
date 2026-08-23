#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
#
# Dehuang.Wu
# Copyright (C) 2021-2026 ArtInChip Technology Co., Ltd

import os
import re
import sys
import platform
import argparse
import subprocess
import json
from collections import OrderedDict


def mkimage_get_resource_size(srcdir, cluster_siz):
    total_size = 0
    root_path = srcdir
    for root, dirs, files in os.walk(srcdir):
        for fn in files:
            fpath = os.path.join(root, fn)
            size = os.path.getsize(fpath)
            size = cluster_siz * int(round((size + cluster_siz - 1) / cluster_siz))
            total_size += size
        for d in dirs:
            total_size += cluster_siz
    return total_size


def mkimage_get_part_size(outfile):
    imgname = os.path.basename(outfile)
    partlist = os.path.join(os.path.dirname(outfile), 'partition_file_list.h')
    size = 0
    if not os.path.exists(partlist):
        return 0
    with open(partlist) as f:
        lines = f.readlines()
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(',')
            if len(parts) < 3:
                continue
            name = parts[1].replace('"', '').replace('*', '')
            if any(imgname == re.sub(pat, "", name) for pat in [".enc", ".sparse.enc", ".sparse"]):
                size = int(parts[2])
                return size
    print('Image {} is not used in any partition'.format(imgname))
    print('please check your project\'s image_cfg.json')
    return size


def parse_image_cfg(cfgfile):
    """ Load image configuration file
    Args:
        cfgfile: Configuration file name
    """
    with open(cfgfile, "r") as f:
        lines = f.readlines()
        jsonstr = ""
        for line in lines:
            sline = line.strip()
            if sline.startswith("//"):
                continue
            slash_start = sline.find("//")
            if slash_start > 0:
                jsonstr += sline[0:slash_start].strip()
            else:
                jsonstr += sline
        # Use OrderedDict is important, we need to iterate FWC in order.
        jsonstr = jsonstr.replace(",}", "}").replace(",]", "]")
        cfg = json.loads(jsonstr, object_pairs_hook=OrderedDict)
    return cfg


def check_is_nftl_part(outfile):
    imgname = os.path.basename(outfile)
    partjson = os.path.join(os.path.dirname(outfile), 'partition.json')
    if not os.path.exists(partjson):
        return False

    cfg = parse_image_cfg(partjson)
    nftl_value = cfg["partitions"].get("nftl")
    if nftl_value is None:
        return False

    imgname = os.path.basename(outfile)
    parts = imgname.split('.')
    nftl_part = parts[0] + ':'
    if nftl_part in nftl_value:
        return True
    return False


def run_cmd(cmdstr):
    # print(cmdstr)
    cmd = cmdstr.split(' ')
    ret = subprocess.run(cmd, stdout=subprocess.PIPE)
    if ret.returncode != 0:
        sys.exit(1)


def _fat_lfn_checksum(short_name):
    """Return the VFAT checksum for one 8.3 directory entry."""
    checksum = 0
    for byte in short_name:
        checksum = ((checksum & 1) << 7) + (checksum >> 1) + byte
        checksum &= 0xFF
    return checksum


def _fat_lfn_entries(filename, short_name):
    """Build the long-file-name entries that must precede *short_name*."""
    import struct

    name_data = filename.encode('utf-16le') + b'\x00\x00'
    units = [name_data[index:index + 2]
             for index in range(0, len(name_data), 2)]
    chunks = [units[index:index + 13]
              for index in range(0, len(units), 13)]
    checksum = _fat_lfn_checksum(short_name)
    entries = []

    for index in range(len(chunks), 0, -1):
        units = chunks[index - 1] + [b'\xff\xff'] * (13 - len(chunks[index - 1]))
        entry = bytearray(32)
        entry[0] = index | (0x40 if index == len(chunks) else 0)
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        entry[26:28] = b'\x00\x00'
        entry[1:11] = b''.join(units[0:5])
        entry[14:26] = b''.join(units[5:11])
        entry[28:32] = b''.join(units[11:13])
        entries.append(entry)
    return entries


def _fat_lfn_name(entries):
    """Decode a preceding VFAT entry sequence, or return an empty string."""
    import struct

    if not entries:
        return ''
    chars = []
    for entry in reversed(entries):
        data = entry[1:11] + entry[14:26] + entry[28:32]
        for index in range(0, len(data), 2):
            value = struct.unpack_from('<H', data, index)[0]
            if value == 0:
                return ''.join(chars)
            if value != 0xFFFF:
                chars.append(chr(value))
    return ''.join(chars)


def _fat_short_name(entry):
    """Decode an ASCII 8.3 name for diagnostics and source matching."""
    base = bytes(entry[0:8]).rstrip(b' ').decode('ascii', errors='replace')
    ext = bytes(entry[8:11]).rstrip(b' ').decode('ascii', errors='replace')
    return '{}.{}'.format(base, ext) if ext else base


class _FatImage(object):
    """Small FAT12/16/32 directory writer used to repair mtools Windows LFN output."""

    def __init__(self, image_path):
        import struct

        self.image_path = image_path
        with open(image_path, 'rb') as image:
            self.data = bytearray(image.read())
        self.bytes_per_sector = struct.unpack_from('<H', self.data, 11)[0]
        self.sectors_per_cluster = self.data[13]
        self.reserved_sectors = struct.unpack_from('<H', self.data, 14)[0]
        self.fat_count = self.data[16]
        self.root_entry_count = struct.unpack_from('<H', self.data, 17)[0]
        fat_size16 = struct.unpack_from('<H', self.data, 22)[0]
        total16 = struct.unpack_from('<H', self.data, 19)[0]
        total32 = struct.unpack_from('<I', self.data, 32)[0]
        fat_size32 = struct.unpack_from('<I', self.data, 36)[0]
        self.fat_size = fat_size16 or fat_size32
        self.total_sectors = total16 or total32
        self.root_dir_sectors = ((self.root_entry_count * 32) +
                                 (self.bytes_per_sector - 1)) // self.bytes_per_sector
        self.first_data_sector = (self.reserved_sectors + self.fat_count * self.fat_size +
                                  self.root_dir_sectors)
        data_sectors = self.total_sectors - self.first_data_sector
        cluster_count = data_sectors // self.sectors_per_cluster
        self.fat_type = 12 if cluster_count < 4085 else (16 if cluster_count < 65525 else 32)
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.root_cluster = (struct.unpack_from('<I', self.data, 44)[0]
                             if self.fat_type == 32 else None)

    def _cluster_offset(self, cluster):
        sector = self.first_data_sector + (cluster - 2) * self.sectors_per_cluster
        return sector * self.bytes_per_sector

    def _next_cluster(self, cluster):
        import struct

        if self.fat_type == 16:
            value = struct.unpack_from('<H', self.data, self.fat_offset + cluster * 2)[0]
            return None if value >= 0xFFF8 else value
        if self.fat_type == 32:
            value = struct.unpack_from('<I', self.data, self.fat_offset + cluster * 4)[0] & 0x0FFFFFFF
            return None if value >= 0x0FFFFFF8 else value
        offset = self.fat_offset + cluster + cluster // 2
        value = self.data[offset] | (self.data[offset + 1] << 8)
        value = value >> 4 if cluster & 1 else value & 0x0FFF
        return None if value >= 0x0FF8 else value

    def _directory_offsets(self, start_cluster=None):
        if start_cluster is None and self.fat_type != 32:
            start = (self.reserved_sectors + self.fat_count * self.fat_size) * self.bytes_per_sector
            return list(range(start, start + self.root_entry_count * 32, 32))

        cluster = self.root_cluster if start_cluster is None else start_cluster
        offsets = []
        visited = set()
        while cluster and cluster not in visited:
            visited.add(cluster)
            start = self._cluster_offset(cluster)
            length = self.sectors_per_cluster * self.bytes_per_sector
            offsets.extend(range(start, start + length, 32))
            cluster = self._next_cluster(cluster)
        return offsets

    def entries(self, start_cluster=None):
        """Return directory entries and the slot list, stopping at the end marker."""
        slots = self._directory_offsets(start_cluster)
        result = []
        lfn_entries = []
        end_index = len(slots)
        for index, offset in enumerate(slots):
            entry = self.data[offset:offset + 32]
            if entry[0] == 0:
                end_index = index
                break
            if entry[0] == 0xE5:
                lfn_entries = []
                continue
            if entry[11] == 0x0F:
                lfn_entries.append(entry)
                continue
            result.append({
                'slot_index': index,
                'offset': offset,
                'entry': entry,
                'lfn_entries': lfn_entries,
                'name': _fat_lfn_name(lfn_entries) or _fat_short_name(entry),
                'size': int.from_bytes(entry[28:32], byteorder='little'),
                'is_dir': bool(entry[11] & 0x10),
                'cluster': int.from_bytes(entry[26:28], byteorder='little') |
                           (int.from_bytes(entry[20:22], byteorder='little') << 16),
            })
            lfn_entries = []
        return result, slots, end_index

    def insert_lfn(self, start_cluster, target_slot_index, filename):
        entries, slots, end_index = self.entries(start_cluster)
        target = next((item for item in entries if item['slot_index'] == target_slot_index), None)
        if target is None or target['lfn_entries']:
            return False
        lfn_entries = _fat_lfn_entries(filename, target['entry'][0:11])
        if end_index + len(lfn_entries) >= len(slots):
            raise RuntimeError('No directory entries available for {}'.format(filename))

        for index in range(end_index - 1, target_slot_index - 1, -1):
            source = slots[index]
            dest = slots[index + len(lfn_entries)]
            self.data[dest:dest + 32] = self.data[source:source + 32]
        for index, entry in enumerate(lfn_entries):
            offset = slots[target_slot_index + index]
            self.data[offset:offset + 32] = entry
        return True

    def save(self):
        with open(self.image_path, 'wb') as image:
            image.write(self.data)


def repair_windows_lfn(image_path, source_dir):
    """Repair Chinese VFAT names lost by the bundled Windows mcopy executable.

    The bundled mcopy converts some Unicode names to an 8.3 alias without adding
    VFAT entries.  File contents and sizes are correct, so match only unambiguous
    source files by size and add their standard UTF-16 LFN records in-place.
    """
    fat = _FatImage(image_path)
    repaired = 0

    def repair_directory(source_path, cluster=None):
        nonlocal repaired
        entries, _, _ = fat.entries(cluster)
        source_items = list(os.scandir(source_path))
        for item in source_items:
            if item.is_dir():
                child = next((entry for entry in entries
                              if entry['is_dir'] and entry['name'].casefold() == item.name.casefold()), None)
                if child is not None:
                    repair_directory(item.path, child['cluster'])

        pending = [item for item in source_items
                   if item.is_file() and not item.name.isascii()]
        while pending:
            entries, _, _ = fat.entries(cluster)
            files_by_size = {}
            for entry in entries:
                if not entry['is_dir']:
                    files_by_size.setdefault(entry['size'], []).append(entry)

            progress = False
            for item in pending[:]:
                if any(entry['name'] == item.name for entry in entries):
                    pending.remove(item)
                    progress = True
                    break
                candidates = [entry for entry in files_by_size.get(item.stat().st_size, [])
                              if not entry['lfn_entries']]
                if len(candidates) != 1:
                    raise RuntimeError('Unable to match FAT entry for {}'.format(item.path))
                if fat.insert_lfn(cluster, candidates[0]['slot_index'], item.name):
                    repaired += 1
                    progress = True
                    break
            if not progress:
                raise RuntimeError('Unable to repair FAT names under {}'.format(source_path))

    repair_directory(source_dir)
    if repaired:
        fat.save()
        print('Repaired {} Windows FAT long file names'.format(repaired))


def gen_fatfs(tooldir, srcdir, outimg, volab, imgsiz, sector_siz, cluster):
    sector_cnt = int(imgsiz / sector_siz)
    if platform.system() == 'Linux':
        truncate = 'truncate -s {} {}'.format(imgsiz, outimg)
        run_cmd(truncate)

        mformat = '{}mformat -i {}'.format(tooldir, outimg)
        mformat += ' -M {} -T {} -c {}'.format(sector_siz, sector_cnt, cluster)
        if volab != 'default':
            mformat += ' -v {}'.format(volab)

        run_cmd(mformat)

        if os.path.exists(srcdir) is True:
            mcopy = '{}mcopy -i {} -s {}// ::/'.format(tooldir, outimg, srcdir)
            run_cmd(mcopy)

        # gen sparse format
        img2simg = '{}img2simg {} {}.sparse 1024'.format(tooldir, outimg, outimg)
        run_cmd(img2simg)

    elif platform.system() == 'Windows':
        outimg = outimg.replace(' ', '\\ ')
        outimg = outimg.replace('/', '\\')
        srcdir = srcdir.replace(' ', '\\ ')
        srcdir = srcdir.replace('/', '\\')

        truncate = '{}truncate.exe {} {}'.format(tooldir, outimg, imgsiz)
        run_cmd(truncate)

        mformat = '{}mformat.exe -i {}'.format(tooldir, outimg)
        mformat += ' -M {} -T {} -c {}'.format(sector_siz, sector_cnt, cluster)
        if volab != 'default':
            mformat += ' -v {}'.format(volab)

        run_cmd(mformat)

        if os.path.exists(srcdir) is True:
            mcopy = '{}mcopy.exe -i {} -s {}\\\\ ::/'.format(tooldir, outimg, srcdir)
            run_cmd(mcopy)
            repair_windows_lfn(outimg, srcdir)

        # gen sparse format
        img2simg = '{}img2simg.exe {} {}.sparse 1024'.format(tooldir, outimg, outimg)
        run_cmd(img2simg)


def round_pow2(x):
    cnt = 0
    shift = 64
    last_one = -1
    for i in range(64):
        if (x >> i) & 0x1:
            last_one = i
            cnt += 1
    if last_one < 0:
        return 0

    if cnt > 1:
        last_one += 1
    value = 1 << last_one
    return value


def aic_calc_checksum(start, size):
    offset = 0
    total = 0
    while offset < size:
        val = int.from_bytes(start[offset: offset + 4], byteorder='little', signed=False)
        total = total + val
        offset = offset + 4
    return (~total) & 0xFFFFFFFF


def set_image_info_to_boot_sector(imgfile, total_size, used_size):
    """ Set AIC FAT Image information after BPB structure, start from 0x80
        struct {
            uint32_t magic; "AICF", byte order: 'F''C''I''A'
            uint32_t cksum32;
            uint32_t length; From Magic to data end
            uint32_t version; 1.1
            uint64_t image_size;
            uint64_t used_size;
            uint32_t dirent_sect_id;
            uint32_t dirent_data_len;
        }
    """
    magic = "AICF"
    cksum = 0
    datalen = 4+4+4+4+8+8
    version = int('0x0101', 16)
    image_size = total_size
    dirent_sect_id = 0
    dirent_datalen = 0

    data = magic.encode(encoding="utf-8")
    data = data + cksum.to_bytes(4, byteorder='little', signed=False)
    data = data + datalen.to_bytes(4, byteorder='little', signed=False)
    data = data + version.to_bytes(4, byteorder='little', signed=False)
    data = data + image_size.to_bytes(8, byteorder='little', signed=False)
    data = data + used_size.to_bytes(8, byteorder='little', signed=False)
    data = data + dirent_sect_id.to_bytes(4, byteorder='little', signed=False)
    data = data + dirent_datalen.to_bytes(4, byteorder='little', signed=False)

    # Update cksum
    cksum = aic_calc_checksum(data, len(data))
    imginfo = data[0:4] + cksum.to_bytes(4, byteorder='little', signed=False) + data[8:]
    with open(imgfile, 'rb+') as f:
        f.seek(128)
        f.write(imginfo)
        f.close()


def main(args):
    cluster = int(args.cluster)
    # cluster should be pow of 2
    cluster = round_pow2(cluster)
    sector_siz = int(args.sector)
    calc_mini = True
    strip_siz = True
    inputdir = args.inputdir
    # First priority is pack folder in the same directory with output file
    inputdir_1st = os.path.join(os.path.dirname(args.outfile), inputdir)
    if os.path.exists(inputdir_1st):
        inputdir = inputdir_1st
    if os.path.exists(inputdir) is False:
        print('Info: inputdir {} is not exist.'.format(inputdir))
    if args.auto:
        # No need to strip size in auto mode
        strip_siz = False
    if args.raw:
        # No need to strip size if user select raw image
        strip_siz = False
    if calc_mini:
        cluster_siz = cluster * sector_siz
        data_siz = mkimage_get_resource_size(inputdir, cluster_siz)
        # Size should alignment with cluster size
        data_siz = cluster_siz * int(((data_siz + cluster_siz - 1) / cluster_siz))

    # Total sector: FAT >= 128, FAT32 >= 0x10000
    min_sect_cnt = 128
    part_size = mkimage_get_part_size(args.outfile)

    if args.auto:
        data_clus_cnt = data_siz / cluster_siz
        data_region_sz = data_clus_cnt * cluster_siz
        if data_clus_cnt < 4096:
            # FAT12
            # - BPB_RsvdSecCnt should be 1
            # - BPB_RootEntCnt max 512
            # - FATsz: 2 * FAT
            # - DATA Region: DATA
            fat_siz = (data_region_sz / cluster_siz) * 12 / 8
            rsvd_siz = 1 * sector_siz
            root_ent_cnt = 512 * 32
        elif data_clus_cnt < 65525:
            # FAT16
            # - BPB_RsvdSecCnt should be 1
            # - BPB_RootEntCnt max 512
            # - FATsz: 2 * FAT
            # - DATA Region: DATA
            fat_siz = (data_region_sz / cluster_siz) * 16 / 8
            rsvd_siz = 1 * sector_siz
            root_ent_cnt = 512 * 32
            imgsiz = rsvd_siz + 2 * fat_siz + root_ent_cnt + data_region_sz
        else:
            # FAT32
            # - BPB_RsvdSecCnt fixed 32
            # - BPB_RootEntCnt fixed 0
            # - FATsz: 2 * FAT
            # - DATA Region: DATA
            fat_siz = data_region_sz / cluster_siz * 4
            rsvd_siz = 32 * sector_siz
            root_ent_cnt = 0
            min_sect_cnt = 0x10000
        fat_siz = sector_siz * int((fat_siz + sector_siz - 1) / sector_siz)
        imgsiz = rsvd_siz + 2 * fat_siz + root_ent_cnt + data_region_sz
        # Round to cluster alignment
        imgsiz = cluster_siz * int(((imgsiz + cluster_siz - 1) / cluster_siz))
        if check_is_nftl_part(args.outfile):
            # Space reserved for bad block management in NFTL
            if args.reserved and (int(args.reserved) > 50):
                NFTL_RESERVED = (int(args.reserved) + 1) * 64 * 2048
            else:
                NFTL_RESERVED = 51 * 64 * 2048
            imgsiz = part_size - NFTL_RESERVED
            if imgsiz < 0:
                print(
                    f'Error, partition size: {part_size} '
                    f'is less than NFTL reserved: {NFTL_RESERVED}.'
                )
                sys.exit(1)
            # Round to cluster alignment
            imgsiz = cluster_siz * int(((imgsiz + cluster_siz - 1) / cluster_siz))
    elif args.fullpart:
        if check_is_nftl_part(args.outfile):
            # Space reserved for bad block management in NFTL
            if args.reserved and (int(args.reserved) > 50):
                NFTL_RESERVED = (int(args.reserved) + 1) * 64 * 2048
            else:
                NFTL_RESERVED = 51 * 64 * 2048
            imgsiz = part_size - NFTL_RESERVED
            if imgsiz < 0:
                print(
                    f'Error, partition size: {part_size} '
                    f'is less than NFTL reserved: {NFTL_RESERVED}.'
                )
                sys.exit(1)
        else:
            imgsiz = part_size
        # Round to cluster alignment
        imgsiz = cluster_siz * int(((imgsiz + cluster_siz - 1) / cluster_siz))
    else:
        imgsiz = int(args.imgsize)

    if (imgsiz / sector_siz) < min_sect_cnt:
        imgsiz = min_sect_cnt * sector_siz
    if part_size and imgsiz > part_size:
        print('Error, fatfs image size is larger than partition size: {}.'.format(args.outfile))
        sys.exit(1)

    gen_fatfs(args.tooldir, inputdir, args.outfile, args.volab, imgsiz, sector_siz, cluster)

    clus_cnt = imgsiz / cluster_siz
    if clus_cnt < 65536:
        # FAT16/FAT12, assume it is FAT16, and evaluate the valid data size
        fat_siz = (clus_cnt * 16) / 8
        rsvd_siz = 1 * sector_siz
        root_ent_cnt = 512 * 32
    else:
        # FAT32, evaluate the valid data size
        fat_siz = (clus_cnt * 32) / 8
        rsvd_siz = 32 * sector_siz
        root_ent_cnt = 0
    minimal_siz = rsvd_siz + 2 * fat_siz + root_ent_cnt + 2 * cluster_siz + data_siz
    # Round to cluster alignment
    minimal_siz = cluster_siz * int(((minimal_siz + cluster_siz - 1) / cluster_siz))
    # Set image information for NFTL offline burn
    set_image_info_to_boot_sector(args.outfile, imgsiz, minimal_siz)

    if platform.system() == 'Linux':
        fatupdate = '{}fatupdate {}'.format(args.tooldir, args.outfile)
        run_cmd(fatupdate)
    elif platform.system() == 'Windows':
        fatupdate = '{}fatupdate.exe {}'.format(args.tooldir, args.outfile)
        run_cmd(fatupdate)
    if strip_siz:
        if platform.system() == 'Linux':
            truncate = 'truncate -s {} {}'.format(minimal_siz, args.outfile)
            run_cmd(truncate)
        elif platform.system() == 'Windows':
            truncate = '{}truncate.exe {} {}'.format(args.tooldir, args.outfile, minimal_siz)
            run_cmd(truncate)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--auto", action='store_true',
                        help="auto size of FAT image")
    parser.add_argument("-f", "--fullpart", action='store_true',
                        help="image size is partition size")
    parser.add_argument("-i", "--inputdir", type=str,
                        help="input directory")
    parser.add_argument("-o", "--outfile", type=str,
                        help="output file")
    parser.add_argument("-g", "--imgsize", type=str,
                        help="image size")
    parser.add_argument("-s", "--sector", type=str,
                        help="sector size")
    parser.add_argument("-c", "--cluster", type=str,
                        help="cluster size")
    parser.add_argument("-t", "--tooldir", type=str,
                        help="tool directory")
    parser.add_argument("-v", "--volab", type=str,
                        help="volume label")
    parser.add_argument("-r", "--raw", action='store_true',
                        help="Don't strip FAT image, keep raw image")
    parser.add_argument("-b", "--reserved", type=str,
                        help="reserved block count for NFTL")
    args = parser.parse_args()
    main(args)
