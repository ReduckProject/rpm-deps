#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openEuler SP1-SP4 软件包最新版本查找工具
从 openEuler 官方仓库抓取软件包信息，找出每个包的最新版本并输出到文件
"""

import re
import os
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
ARCH = "aarch64"
BASE_URL = "https://repo.openeuler.org"
MAX_RETRIES = 3
TIMEOUT = 30  # 秒


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

    def __str__(self) -> str:
        return f"{self.name:<40} {self.version:<20} {self.release:<20} {self.arch:<10} {self.package_size:<12} {self.package_time:<18} [{self.sp_version}/{self.repo_type}]"


class OpenEulerPackageFinder:
    """openEuler软件包查找器"""

    def __init__(self, max_workers: int = 10):
        self.max_workers = max_workers
        self.package_map: Dict[str, List[PackageInfo]] = defaultdict(list)
        self.lock = None  # 在多线程中使用

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
        # HTML结构是多行的tr标签：
        # <tr>
        #     <td colspan="2" class="link"><a href="xxx.rpm" ...></a></td>
        #     <td class="size">14.2 KiB</td>
        #     <td class="date">2022-Dec-29 10:22</td>
        # </tr>
        # 使用正则匹配整个tr块
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
                    url = f"{BASE_URL}/{sp_version}/{repo_type}/{ARCH}/Packages/"
                    tasks.append(
                        executor.submit(
                            self.fetch_packages_from_repo, sp_version, repo_type, url
                        )
                    )

                # EPOL 仓库有特殊的子目录结构
                for epol_type in EPOL_TYPES:
                    url = f"{BASE_URL}/{sp_version}/EPOL/{epol_type}/{ARCH}/Packages/"
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

    def generate_report(self, output_file: str = None):
        """生成报告"""
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"openeuler_packages_{timestamp}.txt"

        latest_packages = self.find_latest_packages()

        with open(output_file, "w", encoding="utf-8") as f:
            f.write("=== openEuler SP1-SP4 软件包最新版本报告 ===\n")
            f.write(f"生成时间: {datetime.now()}\n")
            f.write(f"仓库基础URL: {BASE_URL}\n")
            f.write(f"总包数: {len(latest_packages)}\n\n")

            f.write("SP版本覆盖:\n")
            for sp in SP_VERSIONS:
                f.write(f"  - {sp}\n")
            f.write("\n")

            # 统计各SP版本包数量
            sp_count = defaultdict(int)
            for pkg in latest_packages.values():
                sp_count[pkg.sp_version] += 1

            f.write("各SP版本最新包数量:\n")
            for sp in sorted(sp_count.keys()):
                f.write(f"  {sp}: {sp_count[sp]} 个包\n")
            f.write("\n")

            # 按包名排序输出
            f.write("=" * 180 + "\n")
            f.write(f"{'包名':<40} {'版本':<20} {'Release':<20} {'架构':<10} {'大小':<12} {'包时间':<18} {'SP版本/仓库':<30} {'下载地址'}\n")
            f.write("=" * 180 + "\n")

            for name in sorted(latest_packages.keys()):
                pkg = latest_packages[name]
                f.write(f"{pkg.name:<40} {pkg.version:<20} {pkg.release:<20} {pkg.arch:<10} {pkg.package_size:<12} {pkg.package_time:<18} {pkg.sp_version}/{pkg.repo_type:<30} {pkg.download_url}\n")

        print(f"\n报告已生成: {output_file}")
        print(f"总包数: {len(latest_packages)}")
        return output_file


def main():
    print("=== openEuler SP1-SP4 软件包最新版本查找工具 ===")
    print(f"开始时间: {datetime.now().isoformat()}")
    print("正在抓取仓库信息，请稍候...\n")

    finder = OpenEulerPackageFinder(max_workers=10)
    finder.fetch_all_packages()

    print(f"\n总共发现 {len(finder.package_map)} 个不同的软件包")

    # 生成报告
    finder.generate_report()

    print(f"\n完成时间: {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()