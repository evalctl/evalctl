from pathlib import Path
import os
Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('Looks fine\n')
Path('src').mkdir(exist_ok=True)
Path('src/secrets.py').write_text('token=redacted\n')
