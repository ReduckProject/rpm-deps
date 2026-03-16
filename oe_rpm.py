#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openEuler 软件包管理工具
从 openEuler 官方仓库抓取软件包信息，支持更新元数据和下载rpm包

使用说明:
  python open_euler_package_finder.py --arch=x86_64 --update
  python open_euler_package_finder.py --arch=x86_64 --download=zlib
  python open_euler_package_finder.py --arch=x86_64 --download=zlib --update
"""

import re
import os
import sys
import argparse
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, List, Optional
import urllib.request
import urllib.error


# 仓库配置
SP_VERSIONS = [
    "openEuler-22.03-LTS-SP1",
    "openEuler-22.03-LTS-SP2",
    "openEuler-22.03-LTS-SP3",
    "openEuler-22.03-LTS-SP4",
    # "openEuler-24.03-LTS-SP1",
    # "openEuler-24.03-LTS-SP2",
    # "openEuler-24.03-LTS-SP3",
    # "openEuler-24.03-LTS-SP4",
]

REPO_TYPES = ["OS", "everything", "update"]
EPOL_TYPES = ["main", "update"]  # EPOL 有特殊的子目录结构
# DEFAULT_ARCH = "aarch64"
DEFAULT_ARCH = "x86_64"
BASE_URL = "https://repo.openeuler.org"
MAX_RETRIES = 3
TIMEOUT = 100  # 秒

# 目录配置
RPMS_DIR = "oe-rpms"


@dataclass
class PackageInfo:
    """包信息类"""
    name: str
    version: str
    release: str
    arch: str
    sp_version: str
    repo_type: str
    full_name: str
    download_url: str
    package_time: str = ""  # 包的时间
    package_size: str = ""  # 包的大小
    fetch_time: datetime = field(default_factory=datetime.now)

    def __lt__(self, other: "PackageInfo") -> bool:
        """比较版本，用于排序"""
        if self.name != other.name:
            return self.name < other.name

        # 比较版本号
        result = self._compare_versions(self.version, other.version)
        if result != 0:
            return result < 0

        # 版本相同，比较release
        return self.release < other.release

    def _compare_versions(self, v1: str, v2: str) -> int:
        """简单的版本比较"""
        parts1 = v1.split(".")
        parts2 = v2.split(".")

        max_len = max(len(parts1), len(parts2))
        for i in range(max_len):
            p1 = self._parse_int(parts1[i]) if i < len(parts1) else 0
            p2 = self._parse_int(parts2[i]) if i < len(parts2) else 0
            if p1 != p2:
                return p1 - p2
        return 0

    @staticmethod
    def _parse_int(s: str) -> int:
        """安全解析整数"""
        try:
            return int(re.sub(r"[^0-9]", "", s) or "0")
        except ValueError:
            return 0

    @classmethod
    def parse(cls, full_name: str, sp_version: str, repo_type: str, base_url: str, package_time: str = "", package_size: str = "") -> "PackageInfo":
        """解析RPM包名"""
        # 移除.rpm后缀
        if full_name.endswith('.rpm'):
            base = full_name[:-4]
        else:
            base = full_name

        # RPM包名格式: name-version-release.arch.rpm
        # 1. 最后一个.后面是arch
        last_dot = base.rfind('.')
        if last_dot != -1:
            arch = base[last_dot+1:]
            rest = base[:last_dot]
        else:
            arch = "unknown"
            rest = base

        # 2. 从后往前找第一个-，后面是release
        last_hyphen = rest.rfind('-')
        if last_hyphen != -1:
            release = rest[last_hyphen+1:]
            rest2 = rest[:last_hyphen]

            # 3. 再找倒数第二个-，后面是version
            second_last_hyphen = rest2.rfind('-')
            if second_last_hyphen != -1:
                version = rest2[second_last_hyphen+1:]
                name = rest2[:second_last_hyphen]
            else:
                version = rest2
                name = rest2
        else:
            release = "unknown"
            version = "unknown"
            name = rest

        download_url = f"{base_url}{full_name}"

        return cls(
            name=name,
            version=version,
            release=release,
            arch=arch,
            sp_version=sp_version,
            repo_type=repo_type,
            full_name=full_name,
            download_url=download_url,
            package_time=package_time,
            package_size=package_size,
        )

    def to_meta_line(self) -> str:
        """转换为元数据行"""
        return f"{self.name}|{self.version}|{self.release}|{self.arch}|{self.sp_version}|{self.repo_type}|{self.full_name}|{self.download_url}|{self.package_time}|{self.package_size}\n"

    @classmethod
    def from_meta_line(cls, line: str) -> Optional["PackageInfo"]:
        """从元数据行解析"""
        parts = line.strip().split('|')
        if len(parts) >= 8:
            return cls(
                name=parts[0],
                version=parts[1],
                release=parts[2],
                arch=parts[3],
                sp_version=parts[4],
                repo_type=parts[5],
                full_name=parts[6],
                download_url=parts[7],
                package_time=parts[8] if len(parts) > 8 else "",
                package_size=parts[9] if len(parts) > 9 else "",
            )
        return None

    def __str__(self) -> str:
        return f"{self.name:<40} {self.version:<20} {self.release:<20} {self.arch:<10} {self.package_size:<12} {self.package_time:<18} [{self.sp_version}/{self.repo_type}]"


class OpenEulerPackageFinder:
    """openEuler软件包查找器"""

    def __init__(self, arch: str = DEFAULT_ARCH, max_workers: int = 10):
        self.arch = arch
        self.max_workers = max_workers
        self.package_map: Dict[str, List[PackageInfo]] = defaultdict(list)

    def get_meta_file(self) -> str:
        """获取元数据文件路径"""
        return os.path.join(RPMS_DIR, f"{self.arch}_META.txt")

    def get_download_dir(self) -> str:
        """获取下载目录路径"""
        return os.path.join(RPMS_DIR, self.arch)

    def fetch_url(self, url: str, retry_count: int = 0) -> Optional[str]:
        """获取URL内容"""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if retry_count < MAX_RETRIES:
                time.sleep(2)
                return self.fetch_url(url, retry_count + 1)
            return None
        except Exception as e:
            if retry_count < MAX_RETRIES:
                time.sleep(2)
                return self.fetch_url(url, retry_count + 1)
            print(f"抓取失败: {url} - {e}")
            return None

    def fetch_packages_from_repo(self, sp_version: str, repo_type: str, repo_url: str) -> List[PackageInfo]:
        """从仓库获取包列表"""
        packages = []
        print(f"抓取: [{sp_version}] {repo_type} - {repo_url}")

        html = self.fetch_url(repo_url)
        if html is None:
            print(f"跳过: [{sp_version}] {repo_type} - 仓库不存在或无法访问")
            return packages

        # 解析HTML中的rpm链接、大小和时间
        tr_pattern = re.compile(r'<tr>.*?<a[^>]+href="([^"]+\.rpm)"[^>]*>.*?</a>.*?<td class="size">([^<]+)</td>\s*<td class="date">([^<]+)</td>\s*</tr>', re.DOTALL | re.IGNORECASE)

        for match in tr_pattern.finditer(html):
            package_name = match.group(1)
            package_size = match.group(2).strip()
            package_time = match.group(3).strip()

            if ".." in package_name or package_name.startswith("?"):
                continue

            try:
                pkg_info = PackageInfo.parse(
                    package_name, sp_version, repo_type, repo_url, package_time, package_size
                )
                packages.append(pkg_info)
            except Exception:
                pass

        print(f"完成: [{sp_version}] {repo_type} - 获取 {len(packages)} 个包")
        return packages

    def fetch_all_packages(self):
        """抓取所有SP版本的包信息"""
        tasks = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for sp_version in SP_VERSIONS:
                # 普通仓库类型
                for repo_type in REPO_TYPES:
                    url = f"{BASE_URL}/{sp_version}/{repo_type}/{self.arch}/Packages/"
                    tasks.append(
                        executor.submit(
                            self.fetch_packages_from_repo, sp_version, repo_type, url
                        )
                    )

                # EPOL 仓库有特殊的子目录结构
                for epol_type in EPOL_TYPES:
                    url = f"{BASE_URL}/{sp_version}/EPOL/{epol_type}/{self.arch}/Packages/"
                    repo_type = f"EPOL-{epol_type}"
                    tasks.append(
                        executor.submit(
                            self.fetch_packages_from_repo, sp_version, repo_type, url
                        )
                    )

            # 收集结果
            for future in as_completed(tasks):
                try:
                    packages = future.result()
                    for pkg in packages:
                        self.package_map[pkg.name].append(pkg)
                except Exception as e:
                    print(f"任务执行失败: {e}")

    def find_latest_packages(self) -> Dict[str, PackageInfo]:
        """找出每个包的最新版本"""
        latest = {}
        for name, versions in self.package_map.items():
            if versions:
                # 按版本排序，取最新的
                latest[name] = max(versions, key=lambda p: (p.version, p.release))
        return latest

    def save_meta(self):
        """保存元数据到文件"""
        # 确保目录存在
        os.makedirs(RPMS_DIR, exist_ok=True)

        meta_file = self.get_meta_file()
        latest_packages = self.find_latest_packages()

        with open(meta_file, "w", encoding="utf-8") as f:
            f.write(f"# openEuler 软件包元数据\n")
            f.write(f"# 架构: {self.arch}\n")
            f.write(f"# 更新时间: {datetime.now().isoformat()}\n")
            f.write(f"# 总包数: {len(latest_packages)}\n")
            f.write(f"# 格式: name|version|release|arch|sp_version|repo_type|full_name|download_url|package_time|package_size\n")
            f.write("#" + "=" * 100 + "\n")

            for name in sorted(latest_packages.keys()):
                pkg = latest_packages[name]
                f.write(pkg.to_meta_line())

        print(f"\n元数据已保存: {meta_file}")
        print(f"总包数: {len(latest_packages)}")
        return meta_file

    def load_meta(self) -> Dict[str, PackageInfo]:
        """从元数据文件加载"""
        meta_file = self.get_meta_file()
        packages = {}

        if not os.path.exists(meta_file):
            print(f"元数据文件不存在: {meta_file}")
            print("请先使用 --update 参数更新元数据")
            return packages

        with open(meta_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                pkg = PackageInfo.from_meta_line(line)
                if pkg:
                    packages[pkg.name] = pkg

        print(f"已加载元数据: {len(packages)} 个包")
        return packages

    def search_packages(self, keyword: str) -> List[PackageInfo]:
        """模糊搜索包"""
        packages = self.load_meta()
        if not packages:
            return []

        keyword_lower = keyword.lower()
        matched = []

        for name, pkg in packages.items():
            if keyword_lower in name.lower():
                matched.append(pkg)

        # 按包名排序
        matched.sort(key=lambda p: p.name)
        return matched

    def download_package(self, pkg: PackageInfo) -> bool:
        """下载单个包"""
        download_dir = self.get_download_dir()
        os.makedirs(download_dir, exist_ok=True)

        local_path = os.path.join(download_dir, pkg.full_name)

        # 如果已存在，跳过
        if os.path.exists(local_path):
            print(f"已存在，跳过: {pkg.full_name}")
            return True

        print(f"下载: {pkg.full_name} -> {local_path}")

        try:
            req = urllib.request.Request(pkg.download_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT * 3) as response:
                with open(local_path, "wb") as f:
                    f.write(response.read())
            print(f"完成: {pkg.full_name} ({pkg.package_size})")
            return True
        except Exception as e:
            print(f"下载失败: {pkg.full_name} - {e}")
            return False

    def download_packages(self, keyword: str, force_update: bool = False, auto_confirm: bool = False) -> List[PackageInfo]:
        """搜索并下载包"""
        # 如果指定了 --update，先更新元数据
        if force_update:
            print("\n=== 更新元数据 ===")
            self.fetch_all_packages()
            self.save_meta()

        # 搜索匹配的包
        matched = self.search_packages(keyword)

        if not matched:
            print(f"\n未找到匹配 '{keyword}' 的包")
            return []

        print(f"\n=== 找到 {len(matched)} 个匹配的包 ===")
        for pkg in matched:
            print(f"  - {pkg.name:<40} {pkg.version:<20} {pkg.package_size:<12} {pkg.download_url}")

        # 如果超过10个包，提示确认
        if len(matched) > 10 and not auto_confirm:
            print(f"\n警告: 将下载 {len(matched)} 个包，是否继续？")
            try:
                response = input("输入 'y' 确认，其他键取消: ")
                if response.lower() != 'y':
                    print("已取消下载")
                    return []
            except EOFError:
                print("非交互式环境，使用 -y 参数自动确认")
                return []

        # 下载
        print(f"\n=== 开始下载 {len(matched)} 个包 ===")
        success = 0
        failed = 0

        for pkg in matched:
            if self.download_package(pkg):
                success += 1
            else:
                failed += 1

        print(f"\n=== 下载完成 ===")
        print(f"成功: {success}, 失败: {failed}")

        return matched


def main():
    parser = argparse.ArgumentParser(
        description="openEuler 软件包管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --arch=x86_64 --update              # 更新元数据
  %(prog)s --arch=x86_64 --download=zlib       # 下载匹配zlib的包
  %(prog)s --download=zlib --update            # 先更新元数据再下载
  %(prog)s --download=zlib                     # 使用默认架构(aarch64)下载
        """
    )

    parser.add_argument(
        "--arch",
        type=str,
        default=DEFAULT_ARCH,
        help=f"指定架构，默认: {DEFAULT_ARCH}"
    )

    parser.add_argument(
        "--update",
        action="store_true",
        help="更新元数据信息"
    )

    parser.add_argument(
        "--download",
        type=str,
        default=None,
        help="模糊匹配并下载指定的rpm包"
    )

    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="跳过确认提示，自动确认"
    )

    args = parser.parse_args()

    print("=== openEuler 软件包管理工具 ===")
    print(f"架构: {args.arch}")
    print(f"时间: {datetime.now().isoformat()}\n")

    finder = OpenEulerPackageFinder(arch=args.arch, max_workers=10)

    # 确保rpms目录存在
    os.makedirs(RPMS_DIR, exist_ok=True)

    if args.download:
        # 下载模式
        finder.download_packages(args.download, force_update=args.update, auto_confirm=args.yes)
    elif args.update:
        # 仅更新元数据
        print("正在抓取仓库信息，请稍候...\n")
        finder.fetch_all_packages()
        print(f"\n总共发现 {len(finder.package_map)} 个不同的软件包")
        finder.save_meta()
    else:
        # 无参数，显示帮助
        parser.print_help()

    print(f"\n完成时间: {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()