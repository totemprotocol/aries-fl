import asyncio
import base64
import binascii
import json
import logging
import os
import sys
import torch
from data.hospital_learn import hospital_learn

from urllib.parse import urlparse
from uuid import uuid4


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa

from runners.support.agent import DemoAgent, default_genesis_txns
from runners.support.utils import (
    log_json,
    log_msg,
    log_status,
    log_timer,
    prompt,
    prompt_loop,
    require_indy,
)


LOGGER = logging.getLogger(__name__)

HOSPITAL_NAME = os.getenv("HOSPITAL_NAME")


class Hospital1Agent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            HOSPITAL_NAME + " Agent",
            http_port,
            admin_port,
            prefix=HOSPITAL_NAME,
            extra_args=[
                "--auto-accept-invites",
                "--auto-accept-requests",
                "--auto-store-credential",
            ],
            seed=None,
            **kwargs,
        )
        self.hospital_name = HOSPITAL_NAME
        self.regulator_did = "FEgQXGPN7gpbPqAU65weBT"
        self.connection_id = None
        self._connection_ready = asyncio.Future()
        self.cred_state = {}
        self.trusted_researcher_connection_ids = []

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self.log("Connected")
                self._connection_ready.set_result(True)

    async def handle_issue_credential(self, message):
        state = message["state"]
        credential_exchange_id = message["credential_exchange_id"]
        prev_state = self.cred_state.get(credential_exchange_id)
        if prev_state == state:
            return  # ignore
        self.cred_state[credential_exchange_id] = state

        self.log(
            "Credential: state =",
            state,
            ", credential_exchange_id =",
            credential_exchange_id,
        )

        if state == "request_received":
            log_status("#17 Issue credential to X")
            # issue credentials based on the credential_definition_id
            cred_attrs = self.cred_attrs[message["credential_definition_id"]]
            cred_preview = {
                "@type": CRED_PREVIEW_TYPE,
                "attributes": [
                    {"name": n, "value": v} for (n, v) in cred_attrs.items()
                ],
            }
            await self.admin_POST(
                f"/issue-credential/records/{credential_exchange_id}/issue",
                {
                    "comment": f"Issuing credential, exchange {credential_exchange_id}",
                    "credential_preview": cred_preview,
                },
            )

        if state == "offer_received":
            log_status("#15 After receiving credential offer, send credential request")
            await self.admin_POST(
                "/issue-credential/records/" f"{credential_exchange_id}/send-request"
            )

        elif state == "stored":
            # elif state == "credential_received": ??
            self.log("Storing credential in wallet")
            cred_id = message["credential_id"]
            log_status(f"#18.1 Stored credential {cred_id} in wallet")
            resp = await self.admin_GET(f"/credential/{cred_id}")
            log_json(resp, label="Credential details:")
            log_json(
                message["credential_request_metadata"],
                label="Credential request metadata:",
            )
            self.log("credential_id", message["credential_id"])
            self.log("credential_definition_id", message["credential_definition_id"])
            self.log("schema_id", message["schema_id"])

    async def handle_present_proof(self, message):
        state = message["state"]
        presentation_exchange_id = message["presentation_exchange_id"]
        presentation_request = message["presentation_request"]

        log_msg(
            "Presentation: state =",
            state,
            ", presentation_exchange_id =",
            presentation_exchange_id,
        )

        if state == "request_received":
            log_status(
                "#24 Query for credentials in the wallet that satisfy the proof request"
            )

            # include self-attested attributes (not included in credentials)
            credentials_by_reft = {}
            revealed = {}
            self_attested = {}
            predicates = {}

            # select credentials to provide for the proof
            credentials = await self.admin_GET(
                f"/present-proof/records/{presentation_exchange_id}/credentials"
            )
            if credentials:
                for row in credentials:
                    for referent in row["presentation_referents"]:
                        if referent not in credentials_by_reft:
                            credentials_by_reft[referent] = row

            for referent in presentation_request["requested_attributes"]:
                if referent in credentials_by_reft:
                    revealed[referent] = {
                        "cred_id": credentials_by_reft[referent]["cred_info"][
                            "referent"
                        ],
                        "revealed": True,
                    }
                else:
                    self_attested[referent] = self.hospital_name

            for referent in presentation_request["requested_predicates"]:
                if referent in credentials_by_reft:
                    predicates[referent] = {
                        "cred_id": credentials_by_reft[referent]["cred_info"][
                            "referent"
                        ],
                        "revealed": True,
                    }

            log_status("#25 Generate the proof")
            request = {
                "requested_predicates": predicates,
                "requested_attributes": revealed,
                "self_attested_attributes": self_attested,
            }

            log_status("#26 Send the proof to X")
            await self.admin_POST(
                (
                    "/present-proof/records/"
                    f"{presentation_exchange_id}/send-presentation"
                ),
                request,
            )
        elif state == "presentation_received":
            log_status("#27 Process the proof provided by X")
            log_status("#28 Check if proof is valid")
            proof = await self.admin_POST(
                f"/present-proof/records/{presentation_exchange_id}/"
                "verify-presentation"
            )
            if proof["verified"]:
                self.log("Researcher is verified")
                if message["connection_id"] not in self.trusted_researcher_connection_ids:
                    self.trusted_researcher_connection_ids.append(message["connection_id"])


    async def handle_basicmessages(self, message):
        self.log(message)
        # self.log("Received message:", message["content"])

        if message["connection_id"] in self.trusted_researcher_connection_ids:
            self.log("Research is trusted", message["connection_id"])
            cwd = os.getcwd()
            self.log("Open file")
            try:
                f = open(cwd + "/model/untrained_model.pt", "wb+")
                # self.log(bytes.fromhex(message["content"]))
                byte_message = bytes.fromhex(message["content"])
                f.write(byte_message)
                f.close()

            except Exception as e:
                self.log("Error writing file", e)
                return

            self.log("Import file")
            self.log("learning")

            learnt = await hospital_learn()
            self.log("Learnt : ", learnt)

            trained_model = None
            try:
                trained_file = open(cwd + "/model/trained_model.pt", "rb")
                self.log("Trained file open")
                trained_model = trained_file.read()
                trained_file.close()
            except:
                self.log("Unable to open trained model")

            connection_id = message["connection_id"]

            log_msg("Connection ID", message["connection_id"])
            if trained_model:
                await self.admin_POST(
                    f"/connections/{connection_id}/send-message", {"content": trained_model.hex()}
                )
        else:
            self.log("Untrusted Researcher - Must first authenticate as being certified by Regulator")




async def input_invitation(agent):
    async for details in prompt_loop("Invite details: "):
        b64_invite = None
        try:
            url = urlparse(details)
            query = url.query
            if query and "c_i=" in query:
                pos = query.index("c_i=") + 4
                b64_invite = query[pos:]
            else:
                b64_invite = details
        except ValueError:
            b64_invite = details

        if b64_invite:
            try:
                invite_json = base64.urlsafe_b64decode(b64_invite)
                details = invite_json.decode("utf-8")
            except binascii.Error:
                pass
            except UnicodeDecodeError:
                pass

        if details:
            try:
                json.loads(details)
                break
            except json.JSONDecodeError as e:
                log_msg("Invalid invitation:", str(e))

    with log_timer("Connect duration:"):
        connection = await agent.admin_POST("/connections/receive-invitation", details)
        agent.connection_id = connection["connection_id"]
        log_json(connection, label="Invitation response:")

        await agent.detect_connection()


async def main(start_port: int, show_timing: bool = False):

    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)

    agent = None

    try:
        log_status("#7 Provision an agent and wallet, get back configuration details")
        agent = Hospital1Agent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)

        with log_timer("Startup duration:"):
            await agent.start_process()
        log_msg("Admin url is at:", agent.admin_url)
        log_msg("Endpoint url is at:", agent.endpoint)

        log_status("#9 Input the invitation details")
        await input_invitation(agent)

        async for option in prompt_loop(
            "(1) Request Proof of Certified Researcher \n" +
            "(2) Input New Invitation \n" +
            "(3) List trusted researcher connections \n" +
            "(X) Exit? \n" +
            "[1/2/3/X]: "
        ):
            if option is None or option in "xX":
                break
            elif option == "1":
                log_status("#20 Request proof of Research Certification")
                req_attrs = [
                    {"name": "date", "restrictions": [{"issuer_did": agent.regulator_did}]},
                    {"name": "institution", "restrictions": [{"issuer_did": agent.regulator_did}]},
                ]
                indy_proof_request = {
                    "name": "Proof of Verified Research Institution",
                    "version": "1.0",
                    "nonce": str(uuid4().int),
                    "requested_attributes": {
                        f"0_{req_attr['name']}_uuid": req_attr for req_attr in req_attrs
                    },
                    "requested_predicates": {
                    },
                }
                print("Asking for this proof: ", indy_proof_request)
                proof_request_web_request = {
                    "connection_id": agent.connection_id,
                    "proof_request": indy_proof_request,
                }
                await agent.admin_POST(
                    "/present-proof/send-request", proof_request_web_request
                )

            elif option == "2":
                # handle new invitation
                log_status("Input new invitation details")
                await input_invitation(agent)
            elif option == "3":
                log_status("Trusted Research Connections")
                log_msg(agent.trusted_researcher_connection_ids)

        if show_timing:
            timing = await agent.fetch_timing()
            if timing:
                for line in agent.format_timing(timing):
                    log_msg(line)

    finally:
        terminated = True
        try:
            if agent:
                await agent.terminate()
        except Exception:
            LOGGER.exception("Error terminating agent:")
            terminated = False

    await asyncio.sleep(0.1)

    if not terminated:
        os._exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Runs an Hospital 1 demo agent.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8050,
        metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    args = parser.parse_args()

    require_indy()

    try:
        asyncio.get_event_loop().run_until_complete(main(args.port, args.timing))
    except KeyboardInterrupt:
        os._exit(1)
