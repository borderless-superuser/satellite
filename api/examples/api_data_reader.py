#!/usr/bin/env python
"""
Read Satellite API data received via Blockstream Satellite
"""

import os, sys, argparse, textwrap, struct, zlib, time, logging
from datetime import datetime
import gnupg
import pipe


# Header of the output data structure generated by the Blockstream Satellite
# Receiver prior to writing the incoming data into the API named pipe, for each
# individual message transmitted via API
OUT_DATA_HEADER_FORMAT     = '64sQ'
OUT_DATA_HEADER_LEN        = 64 + 8
OUT_DATA_DELIMITER         = 'vyqzbefrsnzqahgdkrsidzigxvrppato' + \
                             '\xe0\xe0$\x1a\xe4["\xb5Z\x0bv\x17\xa7\xa7\x9d' + \
                             '\xa5\xd6\x00W}M\xa6TO\xda7\xfaeu:\xac\xdc'
DELIMITER_LEN              = len(OUT_DATA_DELIMITER)

# Example user-specific message header (see `api_data_sender.py`)
USER_HEADER_FORMAT         = '255sxi'
USER_HEADER_LEN            = 255 + 1 + 4

#  Other constants
MAX_READ                   = 2**16
DOWNLOAD_DIR               = "downloads"


def save_file(data, filename=None):
    """Save data into a file

    Save given sequence of octets into a file with given name. If the name is
    not specified, use a timestamp as the file name.

    Args:
        data     : Data to save
        filename : Name of the file to save (optional)

    """
    # Save file into a specific directory
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    if (filename is None):
        filename = time.strftime("%Y%m%d%H%M%S")

    # Write file with user data
    f = open(os.path.join(DOWNLOAD_DIR, filename), 'wb')
    f.write(data)
    f.close()

    print("Saved in %s." %(os.path.join(DOWNLOAD_DIR, filename)))


def parse_user_data(data):
    """Parses the user-specific protocol data structure

    Parses the user-specific data structure generated by the
    "api_data_sender.py" example application. Unpacks the CRC32 checksum and the
    file name of the user-specific header. Then, validates the data integrity
    using the checksum and saves the file with the given file name.

    NOTE: the user specific header should not be confused with the header of the
    structure that is output by the Blockstream Satellite Receiver (blocksat-rx
    application) into the API named pipe. The latter is a 40-bytes header
    composed by a delimiter and a length field. Meanwhile, the former is
    user-specific and here corresponds to the protocol created in the
    "api_data_sender.py" example application. For the parsing of the blocksat-rx
    output data structure, see the function below.

    Args:
        data : Sequence of bytes with the raw received data buffer

    """

    # Parse the user-specific header
    user_header = struct.unpack(USER_HEADER_FORMAT, data[:USER_HEADER_LEN])
    filename    = user_header[0].rstrip('\0')
    checksum    = user_header[1]

    # Validate data integrity
    user_data  = data[USER_HEADER_LEN:]
    data_crc32 = zlib.crc32(user_data)

    if (data_crc32 != checksum):
        raise ValueError("Checksum (%d) does not match the header value (%d)" %(
            data_crc32, checksum
        ))

    print("File: %s\tChecksum: %d\tSize: %d bytes" %(
        filename, checksum, len(user_data)))

    save_file(user_data, filename)


def parse_api_out_data(rd_buffer):
    """Parses the structure written by the Blocksat receiver into the API pipe

    Searches the data structure that is output by the Blockstream Satellite
    receiver into the named pipe that is used specifically for API data (at
    /tmp/blockast/api) and retrieves the associated data.

    The 40-bytes structure is as follows:

    {
        char[32] delimiter;
        uint64 length;
    }

    This function first looks for the delimiter that marks the beginning of the
    structure. Once found, it reads the data length and then proceeds to getting
    the actual data payload.

    The delimiter is introduced by the Blockstream Satellite receiver
    application in between segments of data from independent transmissions
    requested via the Satellite API. That is, if two users (e.g. users A and B)
    request transmission at the same time, the resulting stream of bytes in the
    named pipe will have two delimiter sequences, one before the data sent by
    user A and another for the data sent by user B. Importantly, the delimiter
    is guaranteed to exist in the pipe, since it is generated by the receiver
    application (i.e. it cannot be lost in the satellite link).

    Args:
        rd_buffer : Buffer of bytes read from the named pipe

    Returns:
        data : Sequence of bytes containing the output data structure

    """

    # Is there enough data for at least a header?
    if (len(rd_buffer) < OUT_DATA_HEADER_LEN):
        logging.debug("Read buffer has %d bytes - read again" %(
            len(rd_buffer)
        ))
        return []

    # Look for the delimiter
    if (rd_buffer[:DELIMITER_LEN] == OUT_DATA_DELIMITER):
        logging.debug("Delimiter found")
    else:
        raise RuntimeError("Could not find data delimiter")

    # Parse the data header
    header_data = struct.unpack(OUT_DATA_HEADER_FORMAT,
                                rd_buffer[:OUT_DATA_HEADER_LEN])

    # Check the data length given in the header
    data_length = header_data[1]

    logging.debug("Incoming data structure has %d bytes" %(data_length))

    # Do we have all the data already?
    if (len(rd_buffer) < data_length):
        logging.debug("Read buffer has %d bytes - read again" %(
            len(rd_buffer)
        ))
        return []
    else:
        logging.debug("Total data length is ready in the read buffer")

    # Select the bytes corresponding to the actual data
    data_start = OUT_DATA_HEADER_LEN
    data_end   = OUT_DATA_HEADER_LEN + data_length
    data       = rd_buffer[data_start : data_end]

    return data


def main():
    parser = argparse.ArgumentParser(
        description=textwrap.dedent('''\
        Example data reader application

        Continuously reads data in the named pipe that receives the API output
        of the Blockstream Satellite receiver application and waits until a
        complete API data transmission is acquired. Then, by default attempts to
        decrypt the data using the local GnuPG key. On successful decryption,
        validates the integrity of the data and then saves the file in the
        "downloads/" directory. By default, assumes the data was transmitted
        using the example "API data sender" application.

        '''),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-p', '--pipe',
                        default='/tmp/blocksat/api',
                        help='Pipe on which API data is received ' +
                        '(default: /tmp/blocksat/api)')
    parser.add_argument('-g', '--gnupghome', default=".gnupg",
                        help='GnuPG home directory (default: .gnupg)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--save-raw', default=False,
                       action="store_true",
                       help='Save the raw decrypted data in the ' +
                       '\"downloads/\" folder while ignoring the ' +
                       'existence of a user-specific data structure. ' +
                       'Individual API transmissions that can be decrypted '
                       'with the GPG keys you possess ' +
                       'are saved in separate files whose names ' +
                       ' correspond to timestamps. (default: false)')
    group.add_argument('--plaintext', default=False,
                       action="store_true",
                       help='Do not try to decrypt the data. Instead, assume ' +
                       'that all API data transmissions are plaintext and ' +
                       'save them as individual files named by timestamps ' +
                       ' in the  \"downloads/\" folder. ' +
                       'NOTE: this saves all transmissions in the ' +
                       ' \"downloads/\" folder. (default: false)')
    parser.add_argument('--debug', action='store_true',
                        help='Debug mode (default: false)')
    args      = parser.parse_args()
    pipe_file = args.pipe
    gnupghome = args.gnupghome
    save_raw  = args.save_raw
    plaintext = args.plaintext

    # Switch debug level
    if (args.debug):
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
        logging.debug('[Debug Mode]')

    # Open pipe
    pipe_f = pipe.Pipe(pipe_file)

    # GPG object
    if (not plaintext):
        gpg = gnupg.GPG(gnupghome = gnupghome)

    # Read the chosen named pipe continuously and append read data to a
    # buffer. Once complete data structures are ready, output them accordingly.
    rd_buffer = b''

    print("Waiting for data...\n")
    while True:
        rd_buffer += pipe_f.read(MAX_READ)

        # Try to find the data structure that is output by the Blockstream
        # Satellite receiver:
        data = parse_api_out_data(rd_buffer)

        # When the complete structure is found, remove the corresponding data
        # from the read buffer, try to decrypt it and then parse the decrypted
        # user data structure in order to retrieve the embedded file
        if (len(data) > 0):
            # Pop data from the read buffer
            rd_buffer = rd_buffer[(OUT_DATA_HEADER_LEN + len(data)):]

            # In plaintext mode, every API transmission is assumed to be
            # plaintext and output as a file to the downloads folder with a
            # timestamp as name.
            if (plaintext):
                print("[%s]: Got %7d bytes\tSaving as plaintext" %(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(data)))
                save_file(data)
                continue
            else:
                # Try to decrypt the data
                decrypted_data = str(gpg.decrypt(data))

            if (len(decrypted_data) > 0):
                print("[%s]: Got %7d bytes\t Decryption: OK    \t" %(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(data)))
                print("Decrypted data has %d bytes" %(len(decrypted_data)))

                # Parse the user-specific data structure. If ignoring the
                # existence of an application-specific data structure, save the
                # raw decrypted data directly to a file.
                if (not save_raw):
                    parse_user_data(decrypted_data)
                else:
                    save_file(decrypted_data)
            else:
                print("[%s]: Got %7d bytes\t Decryption: FAILED\t" %(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    len(data)) + "Message not for us")


if __name__ == '__main__':
    main()
