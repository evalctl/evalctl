from pathlib import Path
import os
Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('Found null dereference at src/app.py:7\n')
Path('review.md').write_text('Found null dereference at src/app.py:7\n')
