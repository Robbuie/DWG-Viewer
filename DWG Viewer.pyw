"""
DWG Viewer — double-click launcher (no console window).

Windows runs .pyw files with pythonw.exe automatically.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from main import main
main()
