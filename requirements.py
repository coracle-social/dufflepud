import sys
import toml

with open(sys.argv[1]) as f:
    result = toml.load(f)

for package, constraint in result['tool']['poetry']['dependencies'].items():
    if package == 'python':
        continue

    if type(constraint) == str:
        print(package)
    else:
        print(f'{package}[{constraint["extras"][0]}]')
