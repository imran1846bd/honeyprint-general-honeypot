"""Data retention and protocol limits independent of optional Docker installation."""
import io
import json
import struct
import tempfile
import unittest
from adapters.klipper.uploads import prepare_upload
from adapters.klipper.websocket_bridge import command_events,read_frame,validate_rpc
from telemetry.artifact_storage import ArtifactStorage


def form(filename='sample.gcode',root='gcodes',content=b'G28\n'):
    body=b'--limit\r\nContent-Disposition: form-data; name="root"\r\n\r\n'+root.encode()+b'\r\n'
    body+=b'--limit\r\nContent-Disposition: form-data; name="file"; filename="'+filename.encode()+b'"\r\n\r\n'+content+b'\r\n--limit--\r\n'
    return body,'multipart/form-data; boundary=limit'


class KlipperBridgeContractTests(unittest.TestCase):
    def test_retain_exact_bytes_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as root:
            storage=ArtifactStorage(root)
            body,ctype=form(content=b'; retained evidence\nG28\n')
            forwarded,_,record=prepare_upload(body,ctype,storage)
            self.assertEqual(storage.read(record['sha256']),b'; retained evidence\nG28\n')
            self.assertIn(b'filename="sample.gcode"',forwarded)
            self.assertEqual(prepare_upload(body,ctype,storage)[2],record)
            self.assertEqual(len(list(storage.root.glob('*.gcode'))),1)
    def test_invalid_root_path_binary_and_large_artifact_are_not_retained(self):
        with tempfile.TemporaryDirectory() as root:
            storage=ArtifactStorage(root)
            for args in ({'root':'config'},{'filename':'../printer.cfg'},{'content':b'G28\0'},
                         {'content':b'G'*1048577}):
                with self.subTest(args=list(args)):
                    body,ctype=form(**args)
                    with self.assertRaises(ValueError):prepare_upload(body,ctype,storage)
            self.assertFalse(list(storage.root.glob('*.gcode')))
    def test_command_evidence_omits_arguments_and_comments(self):
        events=[];command_events('G1 X10 SECRETVALUE\nM112 ; SECRETCOMMENT\nCUSTOM_PRIVATE_SECRET ARG',events.append)
        self.assertEqual([e['metadata']['gcode'] for e in events],['G1','M112','other'])
        self.assertNotIn('SECRET',json.dumps(events))
    def test_websocket_control_frame_and_payload_limits(self):
        mask=b'abcd';payload=b'ping'
        encoded=bytes(v^mask[i%4] for i,v in enumerate(payload))
        self.assertEqual(read_frame(io.BytesIO(bytes([137,132])+mask+encoded)),(True,9,payload))
        for data in (bytes([129,0]),bytes([137,254])+struct.pack('!H',126),
                     bytes([129,255])+struct.pack('!Q',1048577)):
            with self.assertRaises(ValueError):read_frame(io.BytesIO(data))
    def test_rpc_rejects_host_control_and_non_gcode_print_paths(self):
        for method,params in [('machine.reboot',{}),('printer.print.start',{'filename':'../printer.cfg'}),
                              ('server.files.move',{'source':'gcodes/job.gcode','dest':'config/printer.cfg'}),
                              ('server.database.post_item',{'namespace':'moonraker','key':'secret','value':'x'})]:
            with self.subTest(method=method):
                with self.assertRaises(ValueError):validate_rpc(json.dumps({'jsonrpc':'2.0','method':method,'params':params}))
