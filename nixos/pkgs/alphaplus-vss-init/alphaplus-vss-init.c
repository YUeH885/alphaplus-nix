#include <endian.h>
#include <errno.h>
#include <linux/qrtr.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define VSS_COMMON_SERVICE_ID 800
#define QMI_VSS_SERVER_CONNECT_CB_REQ 0x0602
#define QMI_RESP_TIMEOUT_MS 2000
#define VSS_MAX_ATTEMPTS 5
#define VSS_RETRY_DELAY_MS 500

static volatile sig_atomic_t running = 1;

static void on_signal(int sig)
{
	(void)sig;
	running = 0;
}

static long monotonic_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/*
 * Send QMI_VSS_SERVER_CONNECT_CB_REQ_MSG_V02 (msg 0x0602, TLV 0x01 = 1) to the
 * modem's LG vss_common service. Until APSS completes this handshake, MPSS leaves
 * UIM simlock/personalization initialization blocked and keeps SIM apps in
 * 'detected' state.
 */
static int send_vss_connect_cb(uint32_t node, uint32_t port, uint16_t txn)
{
	/* QMI request header (7B) + TLV 0x01 (type=0x01, len=4, val=1 LE) */
	uint8_t req[14] = {
		0x00,
		(uint8_t)(txn & 0xff), (uint8_t)(txn >> 8),
		(uint8_t)(QMI_VSS_SERVER_CONNECT_CB_REQ & 0xff),
		(uint8_t)(QMI_VSS_SERVER_CONNECT_CB_REQ >> 8),
		0x07, 0x00,
		0x01, 0x04, 0x00, 0x01, 0x00, 0x00, 0x00,
	};
	int fd = socket(AF_QIPCRTR, SOCK_DGRAM, 0);
	if (fd < 0) {
		perror("vss-init: rpc socket");
		return -1;
	}

	struct sockaddr_qrtr dst = {
		.sq_family = AF_QIPCRTR,
		.sq_node = node,
		.sq_port = port,
	};
	if (sendto(fd, req, sizeof(req), 0, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
		fprintf(stderr, "vss-init: sendto %u:%u failed: %s\n",
			node, port, strerror(errno));
		close(fd);
		return -1;
	}

	long deadline = monotonic_ms() + QMI_RESP_TIMEOUT_MS;
	for (;;) {
		long remaining = deadline - monotonic_ms();
		if (remaining <= 0) {
			fprintf(stderr, "vss-init: response timeout from %u:%u\n",
				node, port);
			close(fd);
			return -1;
		}

		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		int rc = poll(&pfd, 1, (int)remaining);
		if (rc < 0) {
			if (errno == EINTR && running)
				continue;
			fprintf(stderr, "vss-init: poll failed on %u:%u: %s\n",
				node, port, strerror(errno));
			close(fd);
			return -1;
		}
		if (rc == 0) {
			fprintf(stderr, "vss-init: response timeout from %u:%u\n",
				node, port);
			close(fd);
			return -1;
		}

		uint8_t rsp[256];
		ssize_t n = recvfrom(fd, rsp, sizeof(rsp), 0, NULL, NULL);
		if (n < 0) {
			if (errno == EINTR && running)
				continue;
			fprintf(stderr, "vss-init: recvfrom %u:%u failed: %s\n",
				node, port, strerror(errno));
			close(fd);
			return -1;
		}
		if (n >= 7 && rsp[0] == 0x04) {
			/* Skip unsolicited indications on this socket while awaiting the response. */
			continue;
		}
		if (n < 14) {
			fprintf(stderr, "vss-init: short QMI response (%zd bytes)\n", n);
			close(fd);
			return -1;
		}

		uint16_t rsp_txn = (uint16_t)rsp[1] | ((uint16_t)rsp[2] << 8);
		uint16_t msg_id = (uint16_t)rsp[3] | ((uint16_t)rsp[4] << 8);
		uint16_t tlv_len = (uint16_t)rsp[8] | ((uint16_t)rsp[9] << 8);
		uint16_t qmi_res = (uint16_t)rsp[10] | ((uint16_t)rsp[11] << 8);
		uint16_t qmi_err = (uint16_t)rsp[12] | ((uint16_t)rsp[13] << 8);
		if (rsp[0] != 0x02 || rsp_txn != txn ||
		    msg_id != QMI_VSS_SERVER_CONNECT_CB_REQ ||
		    rsp[7] != 0x02 || tlv_len != 4 || qmi_res != 0) {
			fprintf(stderr,
				"vss-init: connect_cb on %u:%u unexpected cf=0x%02x txn=%u msg=0x%04x tlv=0x%02x len=%u res=%u err=%u\n",
				node, port, rsp[0], rsp_txn, msg_id, rsp[7], tlv_len, qmi_res, qmi_err);
			close(fd);
			return -1;
		}

		close(fd);
		fprintf(stderr, "vss-init: connect_cb succeeded on %u:%u\n", node, port);
		return 0;
	}
}

int main(void)
{
	setvbuf(stderr, NULL, _IOLBF, 0);

	struct sigaction sa = { .sa_handler = on_signal };
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);

	int ctrl_fd = socket(AF_QIPCRTR, SOCK_DGRAM, 0);
	if (ctrl_fd < 0) {
		perror("vss-init: ctrl socket");
		return 1;
	}

	struct sockaddr_qrtr local = { 0 };
	socklen_t slen = sizeof(local);
	if (getsockname(ctrl_fd, (struct sockaddr *)&local, &slen) < 0) {
		perror("vss-init: getsockname");
		close(ctrl_fd);
		return 1;
	}
	local.sq_port = 0;
	if (bind(ctrl_fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
		perror("vss-init: bind");
		close(ctrl_fd);
		return 1;
	}

	struct sockaddr_qrtr ns_addr = {
		.sq_family = AF_QIPCRTR,
		.sq_node = local.sq_node,
		.sq_port = QRTR_PORT_CTRL,
	};
	struct qrtr_ctrl_pkt lookup = {
		.cmd = htole32(QRTR_TYPE_NEW_LOOKUP),
		.server = {
			.service = htole32(VSS_COMMON_SERVICE_ID),
			.instance = 0,
		},
	};
	if (sendto(ctrl_fd, &lookup, sizeof(lookup), 0,
		   (struct sockaddr *)&ns_addr, sizeof(ns_addr)) < 0) {
		perror("vss-init: QRTR_TYPE_NEW_LOOKUP");
		close(ctrl_fd);
		return 1;
	}

	uint16_t txn = 1;
	while (running) {
		struct qrtr_ctrl_pkt pkt;
		ssize_t n = recvfrom(ctrl_fd, &pkt, sizeof(pkt), 0, NULL, NULL);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			perror("vss-init: recvfrom");
			close(ctrl_fd);
			return 1;
		}
		if ((size_t)n < sizeof(pkt))
			continue;

		if (le32toh(pkt.cmd) != QRTR_TYPE_NEW_SERVER)
			continue;

		uint32_t service = le32toh(pkt.server.service);
		uint32_t node = le32toh(pkt.server.node);
		uint32_t port = le32toh(pkt.server.port);
		if (service != VSS_COMMON_SERVICE_ID || port == 0)
			continue;

		for (int attempt = 0; attempt < VSS_MAX_ATTEMPTS && running; attempt++) {
			int rc = send_vss_connect_cb(node, port, txn++);
			if (txn == 0)
				txn = 1;
			if (rc == 0)
				break;
			if (attempt + 1 < VSS_MAX_ATTEMPTS && running)
				poll(NULL, 0, VSS_RETRY_DELAY_MS);
		}
	}

	close(ctrl_fd);
	return 0;
}
