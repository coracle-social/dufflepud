import sys
from dufflepud import s3

for key in sys.argv[1:]:
    s3.delete(key)
