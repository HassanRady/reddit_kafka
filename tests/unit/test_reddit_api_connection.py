import praw


def test_authentication(reddit_client):
    assert reddit_client.read_only, "Authentication failed"


def test_subreddit_retrieval(reddit_client):
    subreddit_name = "python"
    subreddit = reddit_client.subreddit(subreddit_name)
    assert subreddit.display_name == subreddit_name


def test_subreddit_top_posts_retrieval(reddit_client):
    subreddit_name = "python"
    subreddit = reddit_client.subreddit(subreddit_name)
    top_posts = list(subreddit.top(limit=5))
    assert len(top_posts) == 5


def test_stream(reddit_client):
    subreddit_name = "python"
    subreddit = reddit_client.subreddit(subreddit_name)
    submissions = subreddit.stream.submissions()
    sample = next(submissions)
    assert isinstance(sample, praw.models.Submission)
