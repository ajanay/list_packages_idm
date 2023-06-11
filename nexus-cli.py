#!/usr/bin/env python3
#
# A simple tool to upload or download a directory
# to/from a Nexus repository
#
# Project: RRF
# Version: 0.2
#
# Copyright 2023 - EVIDEN
#


import argparse
import datetime
import getpass
import logging
import os
import sys
import subprocess
from os import path


# TODO: load tool conf and repo login/password from a file
class NexusCliConfig:
    upload_url = 'https://nexus.gsissc.myatos.net'
    upload_repo = 'GH_FR_BEZ_RRF_MAVEN2_RHR'

    download_url = 'https://nexus.forge-dc.cloudmi.minint.fr'
    download_repo = 'rrf-myatos.net'

    # This the common groupId prefix for all uploaded files
    groupId_prefix = 'fr.gouv.minint.rrf'

    # Change to False to disable TLS certificate verification
    verify_tls = True
    #verify_tls = False

    verbose = False

CONF = NexusCliConfig()

def main():

    args = parse_args()

    logLevel = logging.INFO
    if 'verbose' in args or CONF.verbose:
        logLevel = logging.DEBUG

    logging.basicConfig(format='%(levelname)-7s - %(message)s', level=logLevel)

    if args['action'] in {'up', 'upload'}:
        do_upload(args)
    else:
        do_download(args)


def do_upload(args):
    directory = args['directory'].strip('/\\')

    logging.info(f'directory={directory}')

    if not path.exists(directory):
        die(f'no such file or directory: {directory}')

    data = {
        'maven2.groupId': get_groupId(args),
        'maven2.version': args.get('version', '1.0')
    }

    if not path.isdir(directory):
        # TODO handle single files
        die('ERROR: target must be a directory')

    if '.' in directory:
        die('dir name must not contain a "." (dot)')

    data['maven2.artifactId'] = path.basename(directory)

    logging.info(f"Nexus artifact path: {data['maven2.groupId'].replace('.', '/')}/{data['maven2.artifactId']}")

    # Prepare curl args and index file entries
    idxEntries = []
    assetIdx = 2
    with os.scandir(args['directory']) as dirScan:
        for file in dirScan:
            if not file.name.startswith('.') and file.is_file():
                parts = file.name.split('.')
                if len(parts) < 2:
                    logging.warning(f'skipping a file without extension: {file.name}')
                else:
                    data[f'maven2.asset{assetIdx}'] = f'@{file.path}'
                    data[f'maven2.asset{assetIdx}.classifier'] = '.'.join(parts[0:-1])
                    data[f'maven2.asset{assetIdx}.extension'] = parts[-1]
                    idxEntries.append(
                        f"{data['maven2.artifactId']}-{data['maven2.version']}-{data[f'maven2.asset{assetIdx}.classifier']}.{data[f'maven2.asset{assetIdx}.extension']}\n")
                    assetIdx += 1
            elif file.is_dir():
                logging.info(f'skipping sub-directory: {file.name}')

    # temp file will serve as the main artifact and index
    idxFilePath = f"{directory}/{data['maven2.artifactId']}.txt"
    with open(idxFilePath, mode='w+') as idxFile:
        data[f'maven2.asset1'] = '@' + idxFilePath
        data[f'maven2.asset1.extension'] = 'txt'
        idxFile.writelines(idxEntries)
        idxFile.close()


    url = f"{CONF.upload_url}/service/rest/v1/components?repository={CONF.upload_repo}"

    try:
        httpResponse, httpStatus, elapsed = run_curl_cmd(args, url, data)
    finally:
        os.remove(idxFilePath)

    if httpStatus != 204:
        die(f'upload failed stats={httpStatus} != 204 response={httpResponse} ({elapsed})')

    logging.info(f'upload OK ({elapsed})')


def do_download(args):
    artifactId = args['directory']
    version = args.get('version', '1.0')

    logging.info(f'downloading to directory={artifactId}-{version} ...')

    destDir = f'{artifactId}-{version}'
    if os.path.exists(destDir):
        if len(os.listdir(destDir)) != 0:
            die(f'destination directory={destDir} exists and not empty')
    else:
        os.mkdir(destDir)

    groupId = get_groupId(args).replace('.', '/')

    baseUrl = f"{CONF.download_url}/repository/{CONF.download_repo}/{groupId}/{artifactId}/{version}"
    idxUrl = f"{baseUrl}/{destDir}.txt"

    fileNames, httpStatus, elapsed = run_curl_cmd(args, idxUrl)

    if httpStatus != 200:
        die(f'download failed: directory or index file not found. Did you upload using this tool ?')

    if len(fileNames) == 0:
        die(f'nothing to download (index file is empty !)')

    fileHashes = [f'{file}.md5' for file in fileNames]

    filesUrl = ''.join([baseUrl, '/', '{', ','.join(fileHashes + fileNames), '}'])

    httpResponse, httpStatus, elapsed = run_curl_cmd(args, filesUrl, outdir=destDir)

    if httpStatus != 200:
        die(f'download failed httpStatus={httpStatus} ({elapsed}) httpResp={httpResponse} ')

    logging.info(f'donwload OK ({elapsed})')


def run_curl_cmd(args, url, data=None, outdir=None):
    curlCmd = ['curl', '--parallel', '--write-out', "\n**http_status=%{http_code}\n"]

    auth = get_auth(args)
    if auth:
        curlCmd.extend(['-u', auth])

    if not CONF.verify_tls:
        curlCmd.append('--insecure')

    # Upload
    if data:
        for name, value in data.items():
            curlCmd += ['-F', f'{name}={value}']

    # Download
    if outdir:
        # Will retry 6 times with 10s wait between each attempt
        curlCmd.extend(['--remote-name-all', '--retry', '6', '--retry-delay', '10', '--output-dir', outdir])

    curlCmd += [url]

    logging.debug(f'curl cmd={curlCmd}')

    start = datetime.datetime.utcnow()
    # TODO: add proxy from environnement
    curlOut = subprocess.run(curlCmd, stdout=subprocess.PIPE, stderr=sys.stderr, timeout=2*60*60)
    elapsed = (datetime.datetime.utcnow() - start)

    logging.debug(f'curl => status={curlOut.returncode} stdout={curlOut.stdout} stderr={curlOut.stderr}')

    if curlOut.returncode != 0:
        die(f'curl failed: status={curlOut.returncode} time={elapsed} stdout={curlOut.stdout} stderr={curlOut.stderr}')

    respLines = []
    for line in curlOut.stdout.decode('utf-8').splitlines():
        strippedLine = line.strip()
        if strippedLine:
            respLines.append(strippedLine)

    httpStatus = int(respLines[-1].split('=')[1])

    if httpStatus in {401, 403}:
        die(f'curl failed: status={httpStatus} (authentication error) => check login and password ')

    return respLines[0:-1], httpStatus, elapsed


def die(msg):
    logging.error(msg)
    sys.exit(1)


def get_auth(args):
    auth = None

    if 'login' in args:
        if 'passwd' not in args:
            args['passwd'] = getpass.getpass(prompt='Nexus password: ')
        auth = f"{args['login']}:{args['passwd']}"
    else:
        # Try to get the login/passwd from env variables
        if 'NEXUS_LOGIN' in os.environ:
            args['login'] = os.environ['NEXUS_LOGIN']

            if 'NEXUS_PASSWD' in os.environ:
                args['passwd'] = os.environ['NEXUS_PASSWD']
            else:
                die('NEXUS_PASSWD env variable is not defined')
            return get_auth(args)
    return auth


def get_groupId(args):
    groupId = args['group']
    if CONF.groupId_prefix:
        groupId = CONF.groupId_prefix + '.' + groupId
    return groupId


def parse_args():

    parser = argparse.ArgumentParser(argument_default=argparse.SUPPRESS,
                                     description='Tool to upload/download a directory to/from a Nexus Maven2 repo')

    parser.add_argument('-l', '--login', help='Nexus repo user login (will prompt for password). '
                        + 'Will use env variables: NEXUS_LOGIN and NEXUS_PASSWD if present ')

    parser.add_argument('-g', '--group', required=True, help='sub-groupId (bss, ppr, pro ...). '
                        + f'This will be prefixed with \'{CONF.groupId_prefix}.\'')

    parser.add_argument('-v', '--version', help='artifact version. default to 1.0')

    parser.add_argument('-V', '--verbose', help='enable debug logging', action='store_true')

    parser.add_argument('action', metavar='<upload|download>', choices=['down', 'download', 'up', 'upload'],
                        help="action: up/upload or down/download")

    parser.add_argument('directory', metavar='<directory>',
                        help='file or directory to upload or download (corresponds to the maven artifactId)')

    return vars(parser.parse_args())


if __name__ == "__main__":
    main()
