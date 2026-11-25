// SPDX-License-Identifier: MIT
#include <inttypes.h>
#include <libhexagonrpc/fastrpc.h>
#include <libhexagonrpc/interfaces/remotectl.def>
#include <libhexagonrpc/session.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const struct fastrpc_function_def_interp2 vector_add = {2, 1, 2, 0, 1};

static int check_vectors(int fd, uint32_t handle)
{
	const uint32_t lengths[] = {1, 31, 32, 33, 127, 128, 129, 1024, 4097, 16384};
	const unsigned int iterations = 24;
	uint32_t *left, *right, *output;
	uint32_t random = 0x8150820;
	int result = 1;

	left = malloc(sizeof(*left) * 16384);
	right = malloc(sizeof(*right) * 16384);
	output = malloc(sizeof(*output) * 16384);
	if (!left || !right || !output) {
		perror("Could not allocate test vectors");
		goto out;
	}
	for (unsigned int length = 0; length < sizeof(lengths) / sizeof(lengths[0]); length++) {
		uint32_t count = lengths[length], bytes = count * sizeof(*left);
		uint32_t checksum = 0;

		for (unsigned int iteration = 0; iteration < iterations; iteration++) {
			for (uint32_t i = 0; i < count; i++) {
				random = random * 1664525U + 1013904223U;
				left[i] = random;
				random = random * 1664525U + 1013904223U;
				right[i] = random;
				output[i] = 0xdeadbeef;
			}
			int ret = fastrpc2(&vector_add, fd, handle, count,
					   bytes, left, bytes, right, bytes, output);
			if (ret) {
				fprintf(stderr, "Vector RPC failed: %d\n", ret);
				goto out;
			}
			for (uint32_t i = 0; i < count; i++) {
				uint32_t expected = left[i] + right[i];
				if (output[i] != expected) {
					fprintf(stderr, "Vector mismatch: length=%u iteration=%u index=%u expected=%08x actual=%08x\n",
						count, iteration, i, expected, output[i]);
					goto out;
				}
				checksum += output[i];
			}
		}
		printf("PASS length=%u iterations=%u checksum=%08x\n", count, iterations, checksum);
	}
	result = 0;
out:
	free(output);
	free(right);
	free(left);
	return result;
}

int main(void)
{
	char uri[] = "file:///libalphaplus_cdsp_skel.so?alphaplus_cdsp_skel_handle_invoke&_modver=1.0";
	char error[256] = {0};
	uint32_t handle = 0, loader_result = 0;
	int fd = hexagonrpc_fd_from_env();
	int ret, result;

	if (fd < 0) {
		fprintf(stderr, "Run this client through alphaplus-cdsp-test\n");
		return 1;
	}
	ret = fastrpc2(&remotectl_open_def, fd, REMOTECTL_HANDLE,
		       (uint32_t)sizeof(uri), uri, &handle, &loader_result,
		       (uint32_t)sizeof(error), error);
	if (ret) {
		if (ret == -1)
			perror("Could not invoke DSP library loader");
		else
			fprintf(stderr, "DSP library loader RPC failed: %d\n", ret);
		return 1;
	}
	if (loader_result) {
		fprintf(stderr, "Could not load DSP library: loader=%" PRId32 " error=%.*s\n",
			(int32_t)loader_result, (int)sizeof(error), error);
		return 1;
	}
	printf("DSP library loaded: handle=%u\n", handle);
	result = check_vectors(fd, handle);
	ret = fastrpc2(&remotectl_close_def, fd, REMOTECTL_HANDLE, handle,
		       &loader_result, (uint32_t)sizeof(error), error);
	if (ret) {
		if (ret == -1)
			perror("Could not invoke DSP library close");
		else
			fprintf(stderr, "DSP library close RPC failed: %d\n", ret);
		result = 1;
	} else if (loader_result) {
		fprintf(stderr, "Could not close DSP library: loader=%" PRId32 "\n",
			(int32_t)loader_result);
		result = 1;
	}
	return result;
}
