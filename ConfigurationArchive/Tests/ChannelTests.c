#include "../ArchiveChannel.h"
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
static void pair(int fd[2]) { assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0); }
int main(void) {
    int fd[2]; pair(fd);
    pid_t pid = fork(); assert(pid >= 0);
    if (!pid) {
        close(fd[0]); const char bytes[] = "bounded-frame";
        assert(archive_send(fd[1], bytes, sizeof(bytes)) == 0);
        close(fd[1]); _exit(0);
    }
    close(fd[1]); void *data = NULL; uint32_t size = 0;
    assert(archive_receive(fd[0], &data, &size) == 0);
    assert(size == sizeof("bounded-frame") && memcmp(data, "bounded-frame", size) == 0);
    free(data); assert(archive_finish(fd[0], (int)pid, 0) == 0);
    pair(fd); uint32_t header = htonl(144U * 1024U * 1024U + 1U);
    assert(write(fd[1], &header, 4) == 4);
    assert(archive_receive(fd[0], &data, &size) == -1 && data == NULL);
    close(fd[0]); close(fd[1]);
    pair(fd); header = htonl(100); assert(write(fd[1], &header, 4) == 4);
    close(fd[1]); assert(archive_receive(fd[0], &data, &size) == -1 && data == NULL); close(fd[0]);
    pair(fd); assert(archive_send(fd[0], "", 0) == -1); close(fd[0]); close(fd[1]);
    puts("PASS C channel framing: roundtrip, bound, truncated frame, empty frame, child reap");
    return 0;
}
