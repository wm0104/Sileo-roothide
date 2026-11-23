#include "ArchiveChannel.h"
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/time.h>
#include <unistd.h>
#include <fcntl.h>
#include <spawn.h>
#include <signal.h>
#include <stdlib.h>
#include <errno.h>
#include <arpa/inet.h>
#define LIMIT (144U * 1024U * 1024U)
#ifdef __APPLE__
extern int posix_spawnattr_set_persona_np(const posix_spawnattr_t *, uid_t, uint32_t);
extern int posix_spawnattr_set_persona_uid_np(const posix_spawnattr_t *, uid_t);
extern int posix_spawnattr_set_persona_gid_np(const posix_spawnattr_t *, uid_t);
#endif
static int transfer(int fd, void *p, size_t n, int sending) {
    while (n) {
#ifdef __APPLE__
        ssize_t amount = sending ? send(fd, p, n, 0) : recv(fd, p, n, 0);
#else
        ssize_t amount = sending ? send(fd, p, n, MSG_NOSIGNAL) : recv(fd, p, n, 0);
#endif
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) return -1;
        p = (char *)p + amount; n -= (size_t)amount;
    }
    return 0;
}
int archive_send(int fd, const void *bytes, uint32_t length) {
    if (!length || length > LIMIT) { errno = EMSGSIZE; return -1; }
    uint32_t header = htonl(length);
    return transfer(fd, &header, 4, 1) || transfer(fd, (void *)bytes, length, 1) ? -1 : 0;
}
int archive_receive(int fd, void **bytes, uint32_t *length) {
    *bytes = NULL; *length = 0;
    uint32_t header;
    if (transfer(fd, &header, 4, 0)) return -1;
    uint32_t n = ntohl(header);
    if (!n || n > LIMIT) { errno = EMSGSIZE; return -1; }
    void *data = malloc(n);
    if (!data) return -1;
    if (transfer(fd, data, n, 0)) { free(data); return -1; }
    *bytes = data; *length = n; return 0;
}
int archive_finish(int fd, int child, int cancel) {
    close(fd);
    if (cancel) kill((pid_t)child, SIGKILL);
    int status = 0, result;
    do { result = waitpid((pid_t)child, &status, 0); } while (result < 0 && errno == EINTR);
    return result > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}
int archive_launch(const char *executable, int *channel, int *child) {
#ifndef __APPLE__
    (void)executable; (void)channel; (void)child; errno = ENOTSUP; return -1;
#else
    if (getuid() != 501 || geteuid() != 501) { errno = EPERM; return -1; }
    struct stat info;
    if (lstat(executable, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
        info.st_nlink != 1 || (info.st_mode & 022)) { errno = EPERM; return -1; }
    int pair[2] = {-1, -1};
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair)) return -1;
    int error = 0;
    for (int i = 0; i < 2; i++) {
        // Move all sources above the fixed destination; avoids addclose/dup2 aliasing.
        int next = fcntl(pair[i], F_DUPFD_CLOEXEC, 10);
        if (next < 0) { error = errno; goto sockets; }
        close(pair[i]); pair[i] = next;
        int one = 1; struct timeval timeout = {120, 0};
        if (setsockopt(pair[i], SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one)) ||
            setsockopt(pair[i], SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
            setsockopt(pair[i], SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) {
            error = errno; goto sockets;
        }
    }
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attributes;
    error = posix_spawn_file_actions_init(&actions);
    if (error) goto sockets;
    error = posix_spawnattr_init(&attributes);
    if (error) goto actions;
#define CHECK(call) do { error = (call); if (error) goto attributes; } while (0)
    CHECK(posix_spawn_file_actions_adddup2(&actions, pair[1], 3));
    CHECK(posix_spawn_file_actions_addclose(&actions, pair[0]));
    CHECK(posix_spawn_file_actions_addclose(&actions, pair[1]));
    CHECK(posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0));
    CHECK(posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0));
    CHECK(posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0));
    CHECK(posix_spawnattr_setflags(&attributes, POSIX_SPAWN_CLOEXEC_DEFAULT));
    CHECK(posix_spawnattr_set_persona_np(&attributes, 99, 1));
    CHECK(posix_spawnattr_set_persona_uid_np(&attributes, 0));
    CHECK(posix_spawnattr_set_persona_gid_np(&attributes, 0));
    char *const args[] = {(char *)executable, NULL};
    char *const env[] = {"PATH=/usr/bin:/bin", "LANG=C", NULL};
    pid_t pid;
    CHECK(posix_spawn(&pid, executable, &actions, &attributes, args, env));
    *channel = pair[0]; *child = (int)pid;
attributes:
    posix_spawnattr_destroy(&attributes);
actions:
    posix_spawn_file_actions_destroy(&actions);
sockets:
    close(pair[1]);
    if (error) { close(pair[0]); errno = error; return -1; }
    return 0;
#endif
}
