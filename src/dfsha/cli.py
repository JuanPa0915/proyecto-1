import argparse
import sys
try:
    from .client import DFShaClient
except ImportError:
    from client import DFShaClient

def main():
    parser = argparse.ArgumentParser(description="DFSha Command Line Interface")
    parser.add_argument('--mds-url', default='https://localhost:8443', help='MDS Server URL')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # Auth commands
    parser_register = subparsers.add_parser('register', help='Register a new user')
    parser_register.add_argument('username')
    parser_register.add_argument('password')
    
    parser_login = subparsers.add_parser('login', help='Login to get token')
    parser_login.add_argument('username')
    parser_login.add_argument('password')
    
    # Hierarchy commands
    parser_ls = subparsers.add_parser('ls', help='List directory contents')
    parser_ls.add_argument('path', default='/', nargs='?')
    
    parser_mkdir = subparsers.add_parser('mkdir', help='Create a directory')
    parser_mkdir.add_argument('path')
    
    parser_rmdir = subparsers.add_parser('rmdir', help='Remove an empty directory')
    parser_rmdir.add_argument('path')
    
    parser_rm = subparsers.add_parser('rm', help='Remove a file')
    parser_rm.add_argument('path')
    
    # File transfer commands
    parser_put = subparsers.add_parser('put', help='Upload a local file to DFSha')
    parser_put.add_argument('local_path')
    parser_put.add_argument('remote_path')
    
    parser_get = subparsers.add_parser('get', help='Download a file from DFSha')
    parser_get.add_argument('remote_path')
    parser_get.add_argument('local_path')
    
    # Lock commands
    parser_lock = subparsers.add_parser('lock', help='Acquire exclusive lock on a file')
    parser_lock.add_argument('path')
    parser_lock.add_argument('--ttl', type=int, default=30, help='Time to live in seconds')
    
    parser_unlock = subparsers.add_parser('unlock', help='Release a lock')
    parser_unlock.add_argument('lock_id')

    args = parser.parse_args()
    client = DFShaClient(args.mds_url)
    
    try:
        if args.command == 'register':
            client.register(args.username, args.password)
            print(f"User {args.username} registered successfully.")
        elif args.command == 'login':
            token = client.login(args.username, args.password)
            print(f"Logged in successfully. Token saved.")
        elif args.command == 'ls':
            entries = client.ls(args.path)
            for entry in entries:
                print(f"{entry['type']}\t{entry['size']}\t{entry['name']}")
        elif args.command == 'mkdir':
            client.mkdir(args.path)
            print(f"Directory {args.path} created.")
        elif args.command == 'rmdir':
            client.rmdir(args.path)
            print(f"Directory {args.path} removed.")
        elif args.command == 'rm':
            client.rm(args.path)
            print(f"File {args.path} removed.")
        elif args.command == 'put':
            client.put(args.local_path, args.remote_path)
            print(f"File {args.local_path} uploaded to {args.remote_path}.")
        elif args.command == 'get':
            client.get(args.remote_path, args.local_path)
            print(f"File {args.remote_path} downloaded to {args.local_path}.")
        elif args.command == 'lock':
            lock_id = client.lock(args.path, args.ttl)
            print(f"Lock acquired. Lock ID: {lock_id}")
        elif args.command == 'unlock':
            client.unlock(args.lock_id)
            print(f"Lock {args.lock_id} released.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
