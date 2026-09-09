from __future__ import annotations

import pkgutil
import sys
import sysconfig
import os
from pathlib import Path


def main():
	output_path = "./stdlib.py"
	stdlib_path = sysconfig.get_path('stdlib')
	os.makedirs(os.path.dirname(output_path), exist_ok=True)

	with open(output_path, 'w', encoding='utf-8') as f:
		f.write('import warnings\n')
		f.write('warnings.filterwarnings("ignore")\n\n')

		modules = []
		for module_info in pkgutil.iter_modules([stdlib_path]):
			m_name = module_info.name

			if m_name.startswith('__') or m_name in (
				'this',
				'antigravity',
				'doctest',
				'pydoc',
				'pydoc_data',
				'unittest',
				'_sysconfigdata__linux_x86_64-linux-gnu',
				'binhex',
				'formatter',
				'imp',
				'lib2to3',
				'symbol',
				'tkinter',
			):
				continue

			modules.append(m_name)

		for m_name in sorted(modules):
			f.write(f'try: import {m_name}\nexcept: pass\n')

main()
