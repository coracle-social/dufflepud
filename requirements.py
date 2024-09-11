import sys
import toml

with open(sys.argv[1]) as f:
    result = toml.load(f)

for package, constraint in result['tool']['poetry']['dependencies'].items():
    if package == 'python':
        continue

    if constraint == '*':
        print(package)
    elif type(constraint) == str:
        print(f'{package}=={constraint[1:]}')
    else:
        print(f'{package}[{constraint["extras"][0]}]=={constraint["version"][1:]}')
