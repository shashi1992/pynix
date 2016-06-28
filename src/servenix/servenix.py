"""Serve nix store objects over HTTP."""
import argparse
import logging
import os
from os.path import exists, isdir, join, basename, dirname
import re
from subprocess import check_output, Popen, PIPE
import sqlite3

from flask import Flask, make_response, send_file, request
import six

from servenix.utils import decode_str, strip_output
from servenix.exceptions import (NoSuchObject, NoNarGenerated,
                                 CouldNotUpdateHash, ClientError)

_HASH_REGEX=re.compile(r"[a-z0-9]{32}")
_PATH_REGEX=re.compile(r"([a-z0-9]{32})-.*")

class NixServer(Flask):
    """Serves nix packages."""
    def __init__(self, nix_store_path, nix_bin_path, nix_state_path,
                 compression_type):
        self._nix_store_path = nix_store_path
        self._nix_state_path = nix_state_path
        self._nix_bin_path = nix_bin_path
        self._db_path = join(self._nix_state_path, "nix", "db", "db.sqlite")
        self._compression_type = compression_type
        # Cache mapping object hashes to store paths.
        self._hashes_to_paths = {}
        # Cache mapping store paths to object info.
        self._paths_to_info = {}
        # A static string telling a nix client what this store serves.
        self._cache_info = "\n".join([
            "StoreDir: {}".format(self._nix_store_path),
            "WantMassQuery: 1",
            "Priority: 30"
        ]) + "\n"
        if self._compression_type == "bzip2":
            self._content_type = "application/x-bzip2"
            self._nar_extension = ".nar.bz2"
        else:
            self._content_type = "application/x-xz"
            self._nar_extension = ".nar.xz"

    def store_path_from_hash(self, store_object_hash):
        """Look up a store path using its hash.

        The name of every object in the nix store starts with a
        32-character hash. We can find the full path by finding an
        object that starts with this hash.

        :param store_object_hash: The 32-character hash prefix of the object.
        :type store_object_hash: ``str``

        :return: The full store path to the object.
        :rtype: ``str``

        :raises: :py:class:`NoSuchObject` if the object isn't in the store.
        """
        if store_object_hash in self._hashes_to_paths:
            return self._hashes_to_paths[store_object_hash]
        store_objects = map(decode_str, os.listdir(self._nix_store_path))
        for obj in store_objects:
            match = _PATH_REGEX.match(obj)
            if match is None:
                continue
            full_path = join(self._nix_store_path, obj)
            _hash = match.group(1)
            self._hashes_to_paths[_hash] = full_path
            if _hash == store_object_hash:
                return full_path
        raise NoSuchObject("No object with hash {} was found."
                           .format(store_object_hash))

    def get_object_info(self, store_path):
        """Given a store path, get some information about the path.

        :param store_path: Path to the object in the store.
        :type store_path: ``str``

        :return: A dictionary of store object information.
        :rtype: ``dict``
        """
        if store_path in self._paths_to_info:
            return self._paths_to_info[store_path]
        # Invoke nix-store with various queries to get package info.
        nix_store_q = lambda option: strip_output([
            "{}/nix-store".format(self._nix_bin_path),
            "--query", option, store_path
        ])
        # Build the compressed version. Compute its hash and size.
        nar_path = self.build_nar(store_path)
        du = strip_output("du -sb {}".format(nar_path))
        file_size = int(du.split()[0])
        file_hash = strip_output("nix-hash --type sha256 --base32 --flat {}"
                                 .format(nar_path))
        # Some paths have corrupt hashes stored in the sqlite
        # database. I'm not sure why this happens, but we check the
        # actual hash using nix-hash and if it doesn't match what
        # `nix-store -q --hash` says, we update the sqlite database to
        # fix the error.
        registered_store_obj_hash = nix_store_q("--hash")
        correct_hash = "sha256:{}".format(strip_output(
            "nix-hash --type sha256 --base32 {}".format(store_path)))
        if correct_hash != registered_store_obj_hash:
            logging.warn("Incorrect hash {} stored for path {}. Updating."
                         .format(registered_store_obj_hash, store_path))
            # Compute the hash without the base32 encoding.
            full_hash_cmd = "nix-hash --type sha256 {}".format(store_path)
            full_hash = strip_output(full_hash_cmd)
            try:
                with sqlite3.connect(self._db_path) as con:
                    con.execute("UPDATE ValidPaths SET hash = '{}' "
                                "WHERE path = 'sha256:{}';"
                                .format(full_hash, store_path))
            except sqlite3.OperationalError as err:
                raise CouldNotUpdateHash(path, registered_store_obj_hash,
                                         correct_hash, err)
        info = {
            "StorePath": store_path,
            "NarHash": correct_hash,
            "NarSize": nix_store_q("--size"),
            "FileSize": str(file_size),
            "FileHash": "sha256:{}".format(file_hash)
        }
        references = nix_store_q("--references").split()
        if references != []:
            info["References"] = " ".join(map(basename, references))
        deriver = nix_store_q("--deriver")
        if deriver != "unknown-deriver":
            info["Deriver"] = basename(deriver)
        self._paths_to_info[store_path] = info
        return info

    def build_nar(self, store_path):
        """Build a nix archive (nar) and return the resulting path."""
        # Construct a nix expression which will produce a nar.
        nar_expr = "".join([
            "(import <nix/nar.nix> {",
            'storePath = "{}";'.format(store_path),
            'hashAlgo = "sha256";',
            'compressionType = "{}";'.format(self._compression_type),
            "})"])

        # Nix-build this expression, resulting in a store object.
        compressed_path = strip_output([
            join(self._nix_bin_path, "nix-build"),
            "--expr", nar_expr, "--no-out-link"
        ])

        # This path will contain a compressed file; return its path.
        contents = map(decode_str, os.listdir(compressed_path))
        for filename in contents:
            if filename.endswith(self._nar_extension):
                return join(compressed_path, filename)
        raise NoNarGenerated(compressed_path, self._nar_extension)

    def make_app(self):
        """Create a flask app and set up routes on it.

        :return: A flask app.
        :rtype: :py:class:`Flask`
        """
        app = Flask(__name__)

        @app.route("/nix-cache-info")
        def nix_cache_info():
            """Return information about the binary cache."""
            return self._cache_info

        @app.route("/<obj_hash>.narinfo")
        def get_narinfo(obj_hash):
            """Given an object's 32-character hash, return information on it.

            The information includes the object's size (uncompressed), sha256
            hash, store path, and reference graph.

            If the object isn't found, return a 404.

            :param obj_hash: First 32 characters of the object's store path.
            :type obj_hash: ``str``
            """
            if _HASH_REGEX.match(obj_hash) is None:
                 return ("Hash {} must match {}"
                         .format(obj_hash, _HASH_REGEX.pattern), 400)
            try:
                store_path = self.store_path_from_hash(obj_hash)
                store_info = self.get_object_info(store_path)
            except NoSuchObject as err:
                return (err.message, 404)
            # Add a few more keys to the store object, specific to the
            # compression type we're serving.
            store_info["URL"] = "nar/{}{}".format(obj_hash,
                                                  self._nar_extension)
            store_info["Compression"] = self._compression_type
            info_string = "\n".join("{}: {}".format(k, v)
                             for k, v in store_info.items()) + "\n"
            return make_response((info_string, 200,
                                 {"Content-Type": "text/x-nix-narinfo"}))

        @app.route("/nar/<obj_hash>{}".format(self._nar_extension))
        def serve_nar(obj_hash):
            """Return the compressed binary from the nix store.

            If the object isn't found, return a 404.

            :param obj_hash: First 32 characters of the object's store path.
            :type obj_hash: ``str``
            """
            try:
                store_path = self.store_path_from_hash(obj_hash)
            except NoSuchObject as err:
                return (err.message, 404)
            nar_path = self.build_nar(store_path)
            return send_file(nar_path, mimetype=self._content_type)

        @app.route("/get-missing-paths")
        def get_missing_paths():
            """Given a list of store paths, return which are not in the store.

            The request must contain JSON containing a single array
            with a list of store path strings. The response will be a
            JSON array containing store paths which were in the
            request but are not in the local nix store.
            """
            paths = request.get_json()
            if not isinstance(paths, list) or \
                    not all(lambda x: isinstance(x, six.string_types), paths):
                raise ClientError("Expected a list of path strings")
            for path in paths:
                pass

        @app.errorhandler(ClientError)
        def handle_invalid_usage(error):
            response = jsonify(error.to_dict())
            response.status_code = error.status_code
            return response

        return app


def _get_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(prog="servenix")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port to listen on.")
    parser.add_argument("--host", default="localhost",
                        help="Host to listen on.")
    parser.add_argument("--compression-type", default="xz",
                        choices=("xz", "bzip2"),
                        help="How served objects should be compressed.")
    return parser.parse_args()


def main():
    """Main entry point."""
    try:
        nix_bin_path = os.environ["NIX_BIN_PATH"]
        assert exists(join(nix_bin_path, "nix-store"))
        # The store path can be given explicitly, or else it will be
        # inferred to be 2 levels up from the bin path. E.g., if the
        # bin path is /foo/bar/123-nix/bin, the store directory will
        # be /foo/bar.
        nix_store_path = os.environ.get("NIX_STORE_PATH",
                                        dirname(dirname(nix_bin_path)))
        assert isdir(nix_store_path), \
            "Nix store directory {} doesn't exist".format(nix_store_path)
        # The state path can be given explicitly, or else it will be
        # inferred to be sibling to the store directory.
        nix_state_path = os.environ.get("NIX_STATE_PATH",
                                        join(dirname(nix_store_path), "var"))
        assert isdir(nix_state_path), \
            "Nix state directory {} doesn't exist".format(nix_state_path)
    except KeyError as err:
        exit("Invalid environment: variable {} must be set.".format(err))
    args = _get_args()
    nixserver = NixServer(nix_store_path=nix_store_path,
                          nix_state_path=nix_state_path,
                          nix_bin_path=nix_bin_path,
                          compression_type=args.compression_type)
    app = nixserver.make_app()
    app.run(port=args.port, host=args.host)

if __name__ == "__main__":
    main()
